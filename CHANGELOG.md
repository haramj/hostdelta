# Changelog

## Unreleased

### Fixed

- Close SQLite fixture connections deterministically so delayed ResourceWarnings
  cannot contaminate unrelated CLI JSON-output assertions in CI.
- Keep all CI matrix jobs running after a failure for complete diagnostics, and
  update checkout/setup-python to pinned Node.js 24 action releases.

- `status` now returns exit code 3 when a configured source has never been collected,
  is stale, or last reported partial/unavailable coverage, even if the latest overall
  cycle succeeded. State snapshots use their own configured cadence.
- Future-dated, invalid and stopped collector heartbeats cannot establish readiness.

### Added

- Additive status JSON fields: `ready`, `heartbeat_assessment`, and per-source
  `configured`, `fresh`, `age_seconds`, `max_age_seconds` and `reason`.
- A contributor roadmap with independently scoped community tasks.

## 0.2.0

### Added

- Supervised foreground collection with per-source transactions, durable offsets,
  deduplication, restart recovery, coverage history and age-based retention.
- Structured application JSONL ingestion and Python logging instrumentation with
  named events, scoped trace/span/run context, operation duration and redaction.
- Durable HTTP request aggregation independent of original log retention.
- OpenStack application-credential authentication and explicit endpoint probes.
- Proxmox resource inventory change summaries and API/quorum observations.
- Same-boot systemd invocation-change detection and sampled outage/recovery bounds.
- Optional TCP socket sampling and netfilter conntrack event subscriptions.
- Archive briefs, event/trace queries, incidents, status and strict configuration.
- Read-only live acceptance verifier and OpenStack/Proxmox validation runbook.
- Systemd user/system templates, release artifacts, checksum generation and CI.

### Changed

- Database schema migrates additively from version 1 to version 2.
- Brief JSON remains schema version 1 with additive incident and coverage fields.
- Journal subprocess output now has a byte budget as well as a time/record budget.

### Compatibility

- Runtime remains Python 3.10+ with no external Python dependencies.
- Live system collectors require Linux. Conntrack is an optional external utility
  requiring separate operating-system privileges.
- Rollback to 0.1 requires the pre-migration database backup.

## 0.1.0

- Initial host snapshots, state comparison, event briefs, SSH hook, HTTP log analysis,
  consumer acknowledgements and explicit agent lifecycle records.
