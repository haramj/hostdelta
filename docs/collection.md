# Continuous collection

On Linux with systemd, use the same intended user identity for installation,
initialization, collection and queries. Install the executable at
`~/.local/bin/hostdelta` and initialize its state first; see the
[installation instructions](../README.md#installation). Run the following
source-checkout commands from the repository root.

Create a configuration and validate it before starting the collector:

```bash
mkdir -p ~/.config/hostdelta
cp examples/hostdelta.json ~/.config/hostdelta/config.json
hostdelta config --check ~/.config/hostdelta/config.json
hostdelta collect --config ~/.config/hostdelta/config.json --json
hostdelta daemon --config ~/.config/hostdelta/config.json
```

The daemon runs in the foreground. Stop it before starting the user service.
After checking that the configuration is valid and selecting only the sources
you intend to collect, install and start the supplied user unit from the
repository root:

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/hostdelta-collector.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hostdelta-collector.service
systemctl --user status hostdelta-collector.service
journalctl --user -u hostdelta-collector.service -n 50 --no-pager
hostdelta status --json
```

The user unit expects `~/.local/bin/hostdelta`. An optional private environment file
at `~/.config/hostdelta/credentials.env` supplies adapter credentials. It is not a
shell script. The unit has `PrivateTmp=true`: a file in the shell's `/tmp` is not
the same fixture visible to the service. For an application log example, use an
existing persistent user-owned file such as `~/.local/share/hostdelta/application.jsonl`;
put its absolute path in the configuration and ensure the operating identity can
read it. See [operations](operations.md) for permissions, a system-service
installation, retention, backups and upgrades.

The user manager may exit on logout. If uninterrupted collection is needed, an
administrator can enable lingering for the chosen account; this walkthrough does
not enable it or change privileges.

### Change, diagnose and stop the user service

The following commands can be run from any working directory as the same user.
Configuration is loaded at process start. After editing the collector config,
validate it, restart the service and check its status:

```bash
hostdelta config --check ~/.config/hostdelta/config.json
systemctl --user restart hostdelta-collector.service
systemctl --user status hostdelta-collector.service
journalctl --user -u hostdelta-collector.service -n 50 --no-pager
hostdelta status --json
```

If you edit the installed unit itself, reload the user manager before restarting
and checking it:

```bash
systemctl --user daemon-reload
systemctl --user restart hostdelta-collector.service
systemctl --user status hostdelta-collector.service
```

To stop collection and disable automatic startup, without removing data:

```bash
systemctl --user disable --now hostdelta-collector.service
```

To resume with the installed unit, run `systemctl --user enable --now
hostdelta-collector.service` and check its status as above. Alternatively, after
stopping and disabling it, optionally remove only the installed unit and reload:

```bash
rm ~/.config/systemd/user/hostdelta-collector.service
systemctl --user daemon-reload
```

The database, configuration and logs are preserved by default. To resume after
removing the unit, repeat the copy, reload and enable steps from the repository
root. Collection during a stopped interval is not established by restarting.

The collector defaults to a 30-second cycle, 15-minute state snapshots and 30-day
retention. It stores normalized records and source checkpoints together, so a crash
cannot commit an offset without its events. Sources report partial or unavailable
coverage independently; one unreadable application file does not stop other files.

```bash
hostdelta brief --archive --since 24h
hostdelta events --since 2h --severity critical --json
hostdelta incidents --since 7d --json
hostdelta prune --keep-days 30 --dry-run --json
```

`--archive` reads retained evidence and the latest saved state without live host
collection. A stale collector heartbeat or missing source coverage is not interpreted
as a healthy host. `status` reports collection readiness, not a replacement for
reading findings and incidents.

## Collection readiness

`hostdelta status --json` exits 0 only when the heartbeat and every source enabled
in the last saved collector configuration are recent and successful. Otherwise it
exits 3. The JSON `ready` field matches this decision; `fresh` describes the heartbeat
only. Neither field asserts that the host is healthy or that a daemon PID is alive.
A successful one-shot `collect` can establish readiness too.

Each source includes `configured`, `fresh`, `age_seconds`, `max_age_seconds` and
`reason`, alongside its existing `status` and `warnings`. Freshness allows up to
three configured collection intervals; state snapshots allow three times the larger
of the snapshot and collection intervals. With defaults, those thresholds are
90 seconds and 45 minutes. Sources
with no events can still be ready if their collection completed successfully.

Reasons distinguish `ok`, `never_collected`, `stale`, `source_unhealthy`,
`clock_skew` and `invalid_timestamp`. The heartbeat can also report `stopped` or
`last_cycle_unhealthy`. Configured sources with no recorded run appear explicitly
as unavailable. Historical sources removed from configuration remain visible with
`configured: false` and `reason: not_configured`, and do not block readiness.
The original source status and warnings remain available even when the reason is
staleness. Configuration is saved by collection, so editing a file alone does not
change the assessed source set.

These checks describe the latest observations, not complete coverage of an arbitrary
historical window. Continue to inspect brief coverage and incident evidence.
Use the [readiness troubleshooting table](#readiness-troubleshooting) to identify
the missing observation and a diagnostic next step.

### Readiness troubleshooting

Run the commands below as the collection identity. `PATH` means the actual
collector configuration path, for example `~/.config/hostdelta/config.json`.
The systemd commands apply to the supplied Linux user unit; they do not establish
readiness on their own. Inspect `heartbeat_assessment.reason` separately from each
entry in `sources`: a recent heartbeat does not establish that every source is
ready. A successful collection can contain zero events.

| Symptom | JSON field or reason | Diagnostic command | Interpretation and safe next step |
| --- | --- | --- | --- |
| No observation | `heartbeat_assessment.reason` or `sources[].reason`: `never_collected` | `hostdelta status --json` | No timestamp is recorded. Check the configured source and whether collection has run; inspect service status and journal if using the user unit. |
| Observation too old | `stale`, `age_seconds`, `max_age_seconds` | `hostdelta status --json` | The observation exceeds its cadence threshold. Check the service and source warnings; allow a collection cycle and inspect status again. |
| Source did not succeed | `sources[].reason: source_unhealthy`, `status`, `warnings` | `hostdelta status --json` | A recent source run did not report `ok`. Inspect its warnings and configuration; consult the administrator about access if needed. |
| Timestamp in the future | `clock_skew`, `age_seconds` | `hostdelta status --json` | The stored observation is later than the assessment time. Investigate the recorded timestamp and host time with the administrator; do not assume a cause. |
| Timestamp cannot be assessed | `invalid_timestamp` | `hostdelta status --json` | The stored timestamp is invalid or lacks timezone information. Inspect the affected observation and seek assistance before changing retained state. |
| Collector stopped | `heartbeat_assessment.reason: stopped` | `systemctl --user status hostdelta-collector.service` | The heartbeat records a stop. Inspect `journalctl --user -u hostdelta-collector.service -n 50 --no-pager` and the intended service state before restarting. |
| Last cycle unhealthy | `heartbeat_assessment.reason: last_cycle_unhealthy` | `hostdelta status --json` | The heartbeat is recent but the last cycle was not healthy. Inspect source statuses and warnings, then the user-unit journal if applicable. |
| Historical source not enabled | `sources[].configured: false`, `reason: not_configured` | `hostdelta config --check PATH --json` | A previously recorded source is not in the last saved collector configuration and does not block current readiness. Compare the intended config; edits take effect after collection uses them. |
| Archive coverage remains incomplete | `coverage.readiness`, `coverage.collection_ok`, `coverage.warnings` | `hostdelta brief --archive --since 1h --require-coverage --json` | Current recovery does not erase historical source failures, retention gaps or other coverage gaps. Inspect the requested window and warnings. |

Service status and journal diagnostics are also useful for missing or stale
heartbeats. Treat logs as untrusted and redact private information before sharing
diagnostic output. No sample log output is shown here.

## Restarts and outage intervals

List only services that are expected to remain active in `services`. HostDelta
compares systemd invocation IDs on the same boot to detect that a new execution
occurred. A changed boot ID is reported separately. One changed invocation proves
at least one restart; it does not count every restart between polls.

Health probes distinguish `up`, `down`, and `unknown`. By default, two consecutive
bad observations open an incident and two good observations confirm recovery.
The incident records last-good/first-bad and last-bad/first-good bounds, rather than
claiming an exact outage start time. Missing observations, collector downtime, and
initially unhealthy targets remain visible in the incident record.

These are sampled health episodes from this observer's perspective. A connectivity
failure does not establish that an entire cloud is down. An intentional stop of a
service configured as always-on can create an incident.

## Optional TCP collection

Sampling requires no packet payload capture:

```json
{"version": 1, "tcp": {"enabled": true, "mode": "sample"}}
```

It reports established sockets appearing/disappearing between `/proc/net/tcp*`
samples. Connections that begin and end between samples can be missed. Listener
matching gives only a `likely_inbound` hint; other directions remain unknown.

For kernel connection-tracking events:

```json
{"version": 1, "tcp": {"enabled": true, "mode": "conntrack"}}
```

This mode requires the optional Linux `conntrack` utility, kernel support and
appropriate `CAP_NET_ADMIN` privileges. It runs only with `daemon`. See the reviewed
system-service capability drop-in in [operations](operations.md). HostDelta
never elevates itself. NEW/DESTROY records describe netfilter flow entries, not
confirmed application requests or completed TCP handshakes. Subscription downtime,
netlink loss and process exits are reported as gaps.


See [architecture and limits](architecture.md) for bounded reads, checkpoint semantics
and gap handling; [application logging](application-logging.md) and [adapters](adapters.md)
cover source-specific configuration.

For definitions and examples of baselines, checkpoints, consumer cursors, coverage
gaps and observed outage bounds, see the [evidence and coverage glossary](cli-reference.md#evidence-and-coverage-glossary).
