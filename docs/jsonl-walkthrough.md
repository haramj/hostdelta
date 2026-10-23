# Collect and query a synthetic JSONL log

Run every Bash block below in order in **one Bash terminal, from the repository
root** (the directory containing `src`, `examples` and `scripts`). Prerequisites:
Linux with systemd, Python 3.10+, and a source checkout. If needed, follow the
[first-run guide](first-run.md) to obtain the checkout. No installed CLI or pip
packages are needed: this exercise builds and uses the portable CLI directly.

This exercise creates a private temporary directory containing its own database,
config, synthetic log and query results. Every CLI invocation specifies this
state explicitly. It does not start a daemon, change an application service, or
contact a cloud account. Host snapshots still read local package, service, network
and system state; keep the resulting database private. Journal, HTTP access-log,
service health, TCP and cloud-adapter collection are disabled for this exercise.

## 1. Build and prepare an isolated workspace

```bash
python3 scripts/build_zipapp.py
DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hostdelta-jsonl.XXXXXXXX")"
export DEMO_DIR
mkdir "$DEMO_DIR/watched"
printf 'hostdelta-jsonl-exercise\n' > "$DEMO_DIR/.exercise"
printf 'Exercise directory: %s\n' "$DEMO_DIR"
```

`mktemp` creates a new private directory. Keep the printed location if you need to
resume in another terminal; do not substitute an existing state directory. The
PATH and your installed HostDelta executable are unchanged.

## 2. Generate the log and write a complete configuration

```bash
PYTHONPATH=src python3 examples/structured_app.py > "$DEMO_DIR/application.jsonl"
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["DEMO_DIR"]).resolve()
records = [json.loads(line) for line in (root / "application.jsonl").read_text().splitlines()]
assert len(records) == 5, "Expected five synthetic application records"
config = {
    "version": 1,
    "journal": False,
    "application_logs": [str(root / "application.jsonl")],
    "http_logs": [],
    "services": [],
    "tcp": {"enabled": False, "mode": "sample"},
    "adapters": [],
}
(root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
print("Generated 5 JSONL records and config:", root / "config.json")
PY
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" config --check "$DEMO_DIR/config.json" --json
```

The JSON contains an expanded absolute path, not the literal string `$DEMO_DIR`.
Validation returns `type: "config"` and normalized defaults without initializing
state. The SDK example emits synthetic `backup-worker` events with current UTC
timestamps. `PYTHONPATH=src` makes the SDK importable from this checkout: installing
a zipapp alone does not install an importable Python library.

Run the remaining steps within 30 minutes so these event timestamps remain inside
the queries. If you pause longer, widen **both** event-query windows consistently
(e.g. to `2h`). Do not regenerate the log between the two collections below.

## 3. Initialize and collect once

```bash
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" init --watch "$DEMO_DIR/watched" --json
collect_code=0
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" collect --config "$DEMO_DIR/config.json" --json > "$DEMO_DIR/collect-first.json" || collect_code=$?
case "$collect_code" in
  0|3) cat "$DEMO_DIR/collect-first.json" ;;
  *) printf 'Collection failed (exit %s); inspect stderr.\n' "$collect_code" >&2; exit "$collect_code" ;;
esac
```

Initialization restricts watched files to the empty exercise directory, but still
captures the other host-state domains. The collector also saves state snapshots;
these are not disabled by `journal: false`. One-shot collection ends on its own.
An exit of 3 means one or more sources reported incomplete coverage; inspect each
entry in `sources`. A state-source gap must not be mistaken for failure to read
our application log. Exit 1 is an error, not a successful collection report.

`init` may discover on-demand access-log paths; this exercise's collector uses
`http_logs: []` and does not ingest them. On-demand and collector settings are
separate. No background collection is installed.

## 4. Query the retained events and read the brief

```bash
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" events --since 30m --service backup-worker --json > "$DEMO_DIR/events-first.json"
cat "$DEMO_DIR/events-first.json"
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" brief --archive --since 30m --json > "$DEMO_DIR/brief.json"
cat "$DEMO_DIR/brief.json"
```

Expect five events with `service: "backup-worker"`, stable `event_id` values and
names including `backup.verify.started`, `http.server.request`,
`backup.checksum.valid`, `backup.verify.completed` and `worker.ready`.

The SDK's `http.server.request` is an **application event**. It does not populate
the separate HTTP access-log aggregate in `requests`. That aggregate is
`not_configured` here. The fresh snapshots also postdate the start of a 30-minute
window, so a missing-baseline warning and `state_diff: null` are expected on this
new database. `coverage.collection_ok` is therefore false even if the JSONL source
succeeded. Adding `--require-coverage` would return 3. Readiness can also become
stale if you wait several minutes after the one-shot collector ends.

An archive brief does not perform live collection or acknowledge consumer cursors.
The absence of errors in five synthetic records does not establish host health.

## 5. Collect unchanged input and verify no duplicates

```bash
collect_code=0
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" collect --config "$DEMO_DIR/config.json" --json > "$DEMO_DIR/collect-second.json" || collect_code=$?
case "$collect_code" in
  0|3) cat "$DEMO_DIR/collect-second.json" ;;
  *) printf 'Collection failed (exit %s); inspect stderr.\n' "$collect_code" >&2; exit "$collect_code" ;;
esac
python3 dist/hostdelta.pyz --state-dir "$DEMO_DIR/state" events --since 30m --service backup-worker --json > "$DEMO_DIR/events-second.json"
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["DEMO_DIR"]).resolve()
def load(name):
    return json.loads((root / name).read_text())

first, second = load("events-first.json"), load("events-second.json")
assert not first["truncated"] and not second["truncated"]
a = [event["event_id"] for event in first["events"]]
b = [event["event_id"] for event in second["events"]]
assert len(a) == len(b) == len(set(a)) == len(set(b)) == 5
assert set(a) == set(b), "Retained IDs changed; check the input and query window"
source = "application:" + str(root / "application.jsonl")
for name, expected in (("collect-first.json", 5), ("collect-second.json", 0)):
    row = next(row for row in load(name)["sources"] if row["source"] == source)
    assert row["status"] == "ok", row
    assert row["inserted"] == expected, row
print("PASS: 5 unique retained events; unchanged input inserted 0 more events")
PY
```

Checkpoints retain the file identity and offset. Reading the unchanged file again
inserts no new application records. Rewriting the file or running the producer
again is a different exercise and can produce new event IDs. This check does not
claim lossless rotation or exactly-once consumer delivery.

## 6. Remove only the exercise data

After inspecting the results, this block deletes the exercise database, log,
config and captured reports. It checks the directory name and marker before
removal; do not point `DEMO_DIR` at other data. It leaves the checkout and built
`dist/hostdelta.pyz` in place. If an earlier step failed, inspect its files first,
then run this same cleanup block from the terminal retaining `DEMO_DIR`.

```bash
python3 - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

root = Path(os.environ["DEMO_DIR"]).resolve()
assert root.parent == Path(tempfile.gettempdir()).resolve(), "Unexpected parent directory"
assert root.name.startswith("hostdelta-jsonl."), "Unexpected exercise directory"
assert (root / ".exercise").read_text() == "hostdelta-jsonl-exercise\n", "Missing exercise marker"
shutil.rmtree(root)
print("Removed exercise directory:", root)
PY
unset DEMO_DIR
```

For an actual application, continue with [application logging](application-logging.md)
and [continuous collection](collection.md). Use the [live-validation runbook](live-validation.md)
for target-environment acceptance; this synthetic exercise is not cloud certification.
