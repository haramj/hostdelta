# CLI reference

Commands below assume an initialized HostDelta installation on the intended operating
identity. Global options precede the subcommand:

```bash
hostdelta --state-dir /private/path/hostdelta brief --since 2h --json
```

The default state directory is `$XDG_STATE_HOME/hostdelta` or
`~/.local/state/hostdelta`. `HOSTDELTA_STATE_DIR` overrides that default; an explicit
`--state-dir` overrides the environment. The directory must be owned by the current
user with mode 0700, and the database uses mode 0600. See [operations](operations.md)
for service identities, permissions and backup procedures.

## Command map

| Command | Purpose |
| --- | --- |
| `init` | Configure on-demand collection and save an initial state baseline |
| `snapshot --label NAME` | Save observed state with an optional unique label |
| `snapshots` | List the newest 100 saved snapshots |
| `diff [BEFORE] [AFTER]` | Compare two saved snapshots; defaults to `HEAD~1` and `HEAD` |
| `brief` | Combine current observed state, recent events and HTTP summaries |
| `brief --archive` | Read retained evidence and the latest saved snapshot without live collection |
| `session` | Display a briefing and record a tracked session start; used by the SSH hook |
| `hook bash` / `hook zsh` | Print the opt-in, time-limited interactive SSH hook |
| `config` / `config --check PATH` | Print collector defaults or validate a configuration file |
| `collect --config PATH` | Run one collection cycle |
| `daemon --config PATH` | Run the foreground collector until stopped |
| `status` | Inspect collector heartbeat and latest per-source coverage |
| `events` | Query retained events, optionally by service, severity or trace ID |
| `incidents` | Query sampled health incidents overlapping a time window |
| `prune --keep-days N` | Apply retention; add `--dry-run` to preview affected rows |
| `backup PATH` | Safely create a point-in-time database backup |
| `ack --consumer NAME --until TIMESTAMP` | Advance a consumer cursor after successful processing |
| `record --actor NAME --kind start\|finish\|failure` | Store an explicitly reported agent lifecycle event |
| `doctor` | Check platform, command availability and initialization readiness |
| `demo` | Render synthetic sample data without reading or initializing the host |

Use `hostdelta COMMAND --help` for accepted flags. Bare `hostdelta` is equivalent
to `hostdelta brief`. Data-oriented commands accept `--json`; brief, diff and demo
also offer `--full` for expanded human-readable output. The daemon emits structured
operational logs to stderr rather than streaming a single CLI result to stdout.

## Snapshot comparison

References can be a numeric snapshot ID, a unique label, `HEAD`, or `HEAD~N`.
`HEAD` is the latest saved observation. **`diff` does not collect a new snapshot.**
Save a post-change snapshot before comparing a deployment:

```bash
hostdelta snapshot --label deploy-42-before
# Run your deployment.
hostdelta snapshot --label deploy-42-after
hostdelta diff deploy-42-before deploy-42-after
```

Labels are unique, begin with a letter, and use letters, digits, dots, underscores
or hyphens. `HEAD` is reserved. Use a new pair of labels for another deployment.

A live brief saves its current state observation; an archive brief does not. A brief
uses the closest saved baseline at or before its event-window start. If the baseline
is older, the report warns that some state differences may predate the event window.
Without a baseline, it reports current state and available events without inventing
a diff. Unavailable domains or a changed file-watch scope are not treated as deletions.

## Time windows

```bash
hostdelta brief --since 30m
hostdelta brief --since 2d
hostdelta brief --since '2026-09-20T10:00:00+09:00'
hostdelta events --since 8h --service backup-worker --json
```

Durations use an integer followed by `s`, `m`, `h`, `d`, or `w`. ISO date/time input
without a timezone is interpreted as local time. Output timestamps are UTC. Events
are selected by `(since, until]`: the start is exclusive and the end is inclusive.
Application-log ingestion requires timezone-aware timestamps or Unix seconds; it
does not apply the CLI's local-time fallback to application records.

`brief --since last-login` refers to the prior session tracked by HostDelta, not an
inferred OS `lastlog` value. The hook records the new session while preserving the
same prior-session window for a subsequent manual `brief --full`. Without tracked
history, HostDelta explicitly falls back to 24 hours. New consumers also start with
a 24-hour window. `--consumer` and `--since` cannot be combined.

Reading a consumer report does not acknowledge it. Pass the exact `window.until`
from a successfully processed report to `ack`. Acknowledgements cannot move backward
or into the future. Late-written records may require overlapping queries after an
acknowledgement. See the [agent contract](agents.md) for cursor isolation and retries.

## Configuration scope

```bash
hostdelta init \
  --watch /etc/systemd/system \
  --watch /opt/my-agent/config \
  --access-log /var/log/nginx/access.log
```

Default watched paths are `/etc/systemd/system`, `/etc/netplan`, and
`/etc/ssh/sshd_config`. Supplying `--watch` or `--access-log` replaces that entire
list; repeat the option to include multiple absolute paths. Omitted lists retain
their previous settings. Initial setup can discover existing standard Nginx/Apache
access logs when no list is configured.

On-demand `init --access-log` paths and daemon `http_logs` are separate configuration
scopes. Configure the latter explicitly for durable ingestion. Daemon configuration
is loaded at process start; restart the collector to apply edits. The default daemon
cycle is 30 seconds, snapshot interval 15 minutes, and retention 30 days.

See [continuous collection](collection.md) for setup and queries,
[application logging](application-logging.md) for formats and normalization, and
[architecture](architecture.md#bounded-work) for scan limits and recovery semantics.

## JSON and exit codes

Successful data results go to stdout. Runtime errors and JSON-mode argument errors
go to stderr as a schema-version-1 error object. A finding/coverage exit can still
include a valid report on stdout; do not discard it merely because the exit is nonzero.

| Exit | Meaning |
| --- | --- |
| `0` | Command succeeded; no requested finding threshold was met |
| `1` | Runtime error or JSON-mode argument error |
| `2` | Requested `--fail-on warning` or `--fail-on critical` threshold met; plain-text argument errors also use 2 |
| `3` | Detected coverage/readiness gap, partial one-shot collection, or failed live acceptance check |
| `130` | Interrupted interactive command |

`brief --require-coverage` returns 3 for detected collection gaps and takes precedence
over `--fail-on`. Unconfigured HTTP collection is a gap under this strict option.
`doctor` and `status` also use 3 for incomplete readiness. Exit 0 is not a guarantee
of complete history or host health: source retention and permissions may hide events.

For the report schema, finding confidence and handling untrusted evidence, see the
[agent contract](agents.md). For production acceptance, use the
[live-validation runbook](live-validation.md).
