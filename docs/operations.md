# Operations guide

## Choose an operating identity

Use the same OS identity for initialization, collection and queries. The state
directory is private and must have mode 0700. Grant read access to selected logs
through your normal group/ACL policy. HostDelta does not add users to groups or
escalate privileges automatically. Journal visibility commonly requires membership
in an appropriate journal-reading group; verify the actual host's configuration.

The user-service template is the simplest deployment. A user manager may exit on
logout. An administrator can enable lingering for the chosen account if uninterrupted
collection is required. Do not run both the old snapshot timer and the new daemon
unless you intentionally want both snapshot schedules.

## Configuration and credentials

1. Copy `examples/hostdelta.json` to `~/.config/hostdelta/config.json`.
2. Add only existing absolute log paths and always-on systemd service names.
3. Add reviewed adapters with environment-variable references.
4. Run `hostdelta config --check PATH`.
5. Run `hostdelta collect --config PATH --json` and resolve unexpected source gaps.
6. Enable the user-service template and verify `hostdelta status --json`.

Configuration is loaded at process start. Restart the service to apply changes.
For adapters, create the optional `~/.config/hostdelta/credentials.env` using a secure
editor, with mode 0600 and only the required variables. Example keys:

```text
OS_APPLICATION_CREDENTIAL_ID=YOUR_ID
OS_APPLICATION_CREDENTIAL_SECRET=YOUR_SECRET
PROXMOX_TOKEN_ID=monitor@pve!hostdelta
PROXMOX_TOKEN_SECRET=YOUR_SECRET
```

Use only the keys needed for your enabled adapters. Never commit the populated file.
The syntax is systemd EnvironmentFile syntax, without `export` or shell commands.
For rotation, update the file and restart the collector. Environment credentials are
still accessible to sufficiently privileged local processes; account isolation matters.

## System service

`examples/systemd/hostdelta-collector-system.service` is a template for an
administrator-managed installation:

- Provision a dedicated `hostdelta` system user/group through your normal process.
- Install the executable as `/usr/local/bin/hostdelta`.
- Put configuration in `/etc/hostdelta/config.json` and optional credentials in
  `/etc/hostdelta/credentials.env`, readable only by the intended operating identity.
- Provision `/var/lib/hostdelta` owned by that identity with mode 0700 and initialize
  using `HOSTDELTA_STATE_DIR=/var/lib/hostdelta` as that identity.
- Grant access to the exact journal/application/HTTP sources needed.
- Install and enable the reviewed system-service template.

The service uses systemd hardening and writes only its state directory. Its
`ProtectHome=true` setting deliberately excludes home-directory log files. Adjust
only the necessary filesystem permissions if your deployment requires other paths.
Query as the same identity with the same state-directory setting; do not relax the
database directory to world-readable to work around permissions.

### Conntrack capability

TCP sampling needs no additional capability but sees only its current network
namespace. Live conntrack subscriptions need the optional `conntrack` binary,
netfilter support and CAP_NET_ADMIN in the relevant namespace. This is a powerful
capability: enable it only for a reviewed system service dedicated to this function.

`examples/systemd/conntrack-capability.conf` is an optional drop-in for the system
service. A user service cannot grant itself this capability. HostDelta does not run
`sudo`, install conntrack, set file capabilities, or modify firewall/conntrack rules.
The subscription is observational, but the operating identity's capability still
requires an administrator's trust decision.

## Monitoring the collector

```bash
hostdelta status --json
hostdelta brief --archive --since 1h --json
journalctl --user -u hostdelta-collector.service --since '1 hour ago'
```

`status.fresh` means a recent cycle completed, not that a PID was authenticated or
that the host is healthy. A one-shot `collect` also updates the heartbeat. Inspect
per-source statuses, warnings and incident findings. `unavailable` and `partial`
are distinct from an empty successful result. Configuration changes can leave old
source/checkpoint entries in status for diagnosis; those sources are marked
`configured: false` and do not block readiness. Use `status.ready` and its exit code
for collection checks, and inspect each source's `reason` when readiness fails.
See [readiness semantics](collection.md#collection-readiness) for thresholds and fields.

The daemon logs structured lifecycle/source-completion messages to stderr. systemd
captures them without contaminating CLI JSON stdout. SIGTERM requests a graceful
stop after the in-flight bounded source; remaining sources are skipped, transactions
already committed are retained, and the final heartbeat records a stop time. The
system-service stop timeout provides a final safety bound.

## Retention and backup

The configured retention policy runs at most hourly. For manual maintenance, stop
the daemon first; a competing retention writer is refused:

```bash
hostdelta prune --keep-days 30 --dry-run --json
hostdelta prune --keep-days 30 --json
```

Expired unacknowledged evidence can be removed. Consumer cursors remain, and briefs
warn if their requested window predates the retention floor. Named snapshots and
open incidents are retained; review their growth periodically. Increasing retention
does not restore previously deleted data.

For a consistent online SQLite backup, use its backup API under the same identity:

```python
import os
import sqlite3

# Use your actual private paths. Run as the state-directory owner.
source = sqlite3.connect('/private/state/hostdelta/state.sqlite3')
fd = os.open('/private/backup/hostdelta.sqlite3', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
destination = sqlite3.connect('/private/backup/hostdelta.sqlite3')
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
```

Do not copy just the main database file while WAL writers are active. Alternatively,
stop all writers and back up the complete private state directory. Raw source logs
are not included in the database backup; only collected normalized evidence is.

## Upgrade and rollback

1. Record the running version, configuration and service unit.
2. Stop the collector and scheduled snapshot writers.
3. Back up the state directory/database using a consistent procedure.
4. Verify checksums and replace the executable.
5. Run the new version's tests and one-shot collection with the same configuration.
6. Verify migration, source coverage and retained cursors; then restart supervision.

Version 0.2 opens version-1 databases with additive schema migration to version 2.
Rollback to 0.1 requires restoring the pre-upgrade backup and old executable together;
the old executable rejects schema 2. Do not manually lower PRAGMA user_version.

## Removal

Stop/disable the service or timer, remove the installed executable and the reviewed
unit files, and reload systemd. Keep the state/configuration for audit or delete them
intentionally. Deleting the state directory removes every snapshot, event, incident
and consumer offset. HostDelta has no remote account or hosted data to delete.
