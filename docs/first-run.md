# Install HostDelta and read your first report

This guide takes you from a source checkout to a local report. Run the numbered
steps in order, in the same Bash or Zsh terminal, as the user who will read the
logs. It installs the CLI for that user and saves local observations; it does not
install a daemon, configure an SSH hook, or change host services.

## 1. Check the environment

**Working directory: any directory.** Live collection requires Linux with systemd
and Python 3.10 or newer. You also need Git and the standard `mkdir` and `install`
utilities. Use an existing host account with access to the selected logs.

```bash
uname -s
python3 --version
git --version
```

For the complete walkthrough, `uname -s` must print `Linux`. Python must report
3.10 or later. Install missing prerequisites using your distribution's normal
administration process before continuing. HostDelta has no third-party Python
runtime dependencies; this installation needs neither pip nor a virtual environment.

On macOS, you can follow steps 2–3 to build and preview the synthetic demo. Stop
before step 4: macOS does not support live host initialization or collection.
A generic Linux container without systemd is not a substitute for the intended
host environment.

## 2. Get the source and install the portable CLI

**Working directory: a parent directory where you want a new `hostdelta` checkout.**
If you already have a checkout, use it instead of cloning into an existing folder;
start the build block from its root. These instructions build the checked-out
revision, so you can record it with `git rev-parse HEAD` when reporting results.

```bash
git clone https://github.com/haramj/hostdelta.git
cd hostdelta
git rev-parse HEAD
```

**Working directory: the `hostdelta` repository root** (the directory containing
`README.md`, `src`, and `scripts`). The `install` command below replaces an existing
`$HOME/.local/bin/hostdelta` executable if present; it does not remove saved state.

```bash
python3 scripts/build_zipapp.py
mkdir -p "$HOME/.local/bin"
install -m 755 dist/hostdelta.pyz "$HOME/.local/bin/hostdelta"
export PATH="$HOME/.local/bin:$PATH"
command -v hostdelta
hostdelta --version
```

The build prints the path to `dist/hostdelta.pyz`. `command -v` should resolve to
`$HOME/.local/bin/hostdelta`, and `--version` prints the built version. If another
command or shell alias wins, invoke `"$HOME/.local/bin/hostdelta"` directly in the
remaining steps. The executable uses `python3` from PATH, so Python must remain
installed. The PATH export affects only this terminal; no shell profile is edited.
In a later terminal, repeat that export or use the absolute executable path.

## 3. Preview without collecting host data

**Working directory: any directory; keep the PATH from step 2.**

```bash
hostdelta demo --json
```

Expect one JSON report with `schema_version: 1`, `type: "brief"`,
`host_mutations: false`, and `local_state_updated: false`. Its events and findings
are synthetic. It does not initialize state or describe this machine's health.
The command exits 0 even though the demo contains example critical findings.

**macOS users: stop here.** The remaining numbered steps are for Linux with systemd.

## 4. Initialize under your operating user

**Working directory: any directory on the Linux host.** Keep the same user and
terminal for all following commands. Do not switch to `sudo` to hide coverage gaps:
a different user has independent state and permissions.

State defaults to `$XDG_STATE_HOME/hostdelta` when `XDG_STATE_HOME` is set, otherwise
`$HOME/.local/state/hostdelta`. `HOSTDELTA_STATE_DIR` overrides that location. To
check existing environment overrides before initialization:

```bash
printenv HOSTDELTA_STATE_DIR XDG_STATE_HOME
```

No output is normal when neither variable is set (`printenv` can return 1 for
unset variables). Keep your intended overrides consistent across commands. An
explicit `--state-dir` overrides both; when using it, put it **before** every
subcommand, as described in the [CLI reference](cli-reference.md).

```bash
hostdelta init --json
```

Expect `type: "init"`, a `snapshot_id`, and the actual `state_dir`. Inspect
`unavailable`: initialization may exit 0 while some state domains could not be
observed. The directory is private to its owner (0700), with a 0600 database.

`init` saves a baseline and discovers common Nginx/Apache access logs when no
access-log configuration exists. It does not create a web server or its logs.
On an existing installation it keeps saved history and, without explicit options,
reuses watch/access-log settings while adding another snapshot. It is not a reset.
A snapshot contains selected observations, not a VM image or restore point.

## 5. Check tools, then read a report

**Working directory: any directory on the same Linux host, under the same user.**
Run these individually so you can inspect each result:

```bash
hostdelta doctor --json
```

Expect `type: "doctor"`, `initialized: true`, and
`live_collection_supported: true`. `commands` lists discovered tools and `ready`
summarizes availability. Exit 3 means prerequisites are incomplete; it does not
mean every source was tested. Even `ready: true` does not verify log permissions.
Inspect the reported missing tool and use the host's normal administration process
if installation is needed; do not treat elevated privileges as the default fix.

```bash
hostdelta brief --since 30m --json
```

Expect `type: "brief"`, `schema_version: 1`, a UTC `window`, `current_state`,
`events`, `requests`, and `coverage`. This live brief saves a new snapshot but
does not acknowledge any consumer cursor. Check `coverage.warnings` and each
state domain's status before interpreting findings.

Immediately after a first initialization, the baseline is newer than the start of
a 30-minute window. In that case `state_diff` is null and a missing-baseline warning
is expected. Retained logs can still supply earlier events; HostDelta cannot invent
older state. Once a snapshot exists at or before a later query's window start,
the brief can compare state. To compare the two snapshots just saved by `init`
and `brief` directly, run:

```bash
hostdelta diff HEAD~1 HEAD --json
```

This compares saved observations and does not collect new state. With no intervening
change, an empty change list can be correct. An unreadable source or unconfigured
HTTP log is a coverage gap, not proof that zero activity occurred. An accessible
source with zero matching records is different from a source that was not read.

For a human-readable view, run `hostdelta brief --since 30m --full`; it performs
another live collection. For exact fields and cursor semantics, see the
[agent contract](agents.md).

## Exit codes and strict coverage

**Working directory: any directory, using the same initialized state.** Optional
strict checking still prints a report when it detects gaps:

```bash
hostdelta brief --since 30m --require-coverage --json
printf 'Exit status: %s\n' "$?"
```

Read the printed exit status, which is captured immediately after `hostdelta`.
Run this interactively without shell `errexit` (`set -e`) if you want to inspect a
nonzero result; automated callers should explicitly handle nonzero statuses.

| Exit | Interpretation |
| --- | --- |
| 0 | The command completed without a requested finding/coverage threshold being met; this does not prove host health or complete history. |
| 1 | Runtime or JSON-mode argument error; inspect the error JSON on stderr. |
| 2 | A requested `--fail-on warning` or `--fail-on critical` threshold was met; the report remains on stdout. Plain-text argument errors also use 2. |
| 3 | Detected coverage/readiness gap (including `doctor` readiness or `brief --require-coverage`). It takes precedence over finding exit 2. |

A newly initialized host can return 3 for a missing baseline, unreadable logs or
unconfigured HTTP collection. Inspect the warnings instead of assuming the command
crashed. Without `--fail-on`, critical findings alone do not change a brief's exit
code. Do not discard valid report JSON solely because the exit status is nonzero.

## Continue from here

This walkthrough performs on-demand collection only. `status` and `brief --archive`
are intended for retained collector evidence; `init` alone does not establish a
collector heartbeat or current configured-source readiness.

- Follow [continuous collection](collection.md) to create and validate a collector
  configuration, run one cycle, and optionally supervise a daemon.
- Follow [application logging](application-logging.md) to collect structured JSONL.
- Use [operations](operations.md) for permissions, backups and upgrades.
- Use [live validation](live-validation.md) for target-environment acceptance;
  completing a first run is not cloud or distribution certification.

The executable and state remain for your next session. No background process was
started by these steps. Do not delete the state directory to resolve a coverage
warning: it contains the observations and cursors needed for later comparisons.
