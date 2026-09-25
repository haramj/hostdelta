# Continuous collection

Run the source-checkout commands below from the repository root after installing
HostDelta and initializing its state under the intended operating identity.


Create a configuration and validate it before starting the collector:

```bash
mkdir -p ~/.config/hostdelta
cp examples/hostdelta.json ~/.config/hostdelta/config.json
hostdelta config --check ~/.config/hostdelta/config.json
hostdelta collect --config ~/.config/hostdelta/config.json --json
hostdelta daemon --config ~/.config/hostdelta/config.json
```

The daemon runs in the foreground. To supervise it as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/hostdelta-collector.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hostdelta-collector.service
hostdelta status --json
```

The user unit expects `~/.local/bin/hostdelta`. An optional private environment file
at `~/.config/hostdelta/credentials.env` supplies adapter credentials. It is not a
shell script. See [operations](operations.md) for permissions, a system-service
installation, uninterrupted collection after logout, retention, backups and upgrades.

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
