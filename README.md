# HostDelta

[![Tests](https://github.com/haramj/hostdelta/actions/workflows/test.yml/badge.svg)](https://github.com/haramj/hostdelta/actions/workflows/test.yml)
[![Contributions welcome](https://img.shields.io/badge/contributions-welcome-brightgreen)](CONTRIBUTING.md)

**Catch up on your server.**

HostDelta is a local change-briefing CLI for Linux hosts and VMs.
It answers “What changed since I last checked?” by combining state differences,
system events, and HTTP request summaries.

Use it when returning over SSH, reviewing a deployment, or giving an operations
agent a structured view of recent host activity. Evidence stays on the host;
no hosted service, LLM, or external Python packages are required at runtime.

**v0.2.0 · Beta · Linux with systemd · Python 3.10+ · MIT**

## What a briefing looks like

Actual output excerpt from `hostdelta demo`. **The demo uses synthetic data, not a
live incident.** Omitted sections are marked with `...`; the remaining lines are
unchanged command output.

```text
HostDelta — catch up on your server
Host: demo-compute-1 (synthetic data)
Since: 2026-09-18T04:00:00.000000+00:00
Until: 2026-09-21T12:00:00.000000+00:00

ATTENTION
  [CRITICAL] Currently failed services: vm-save.service
  [WARNING] HTTP 5xx responses: 18 / 1427
...
State comparison: 2026-09-18T04:00:00.000000+00:00 → 2026-09-21T12:00:00.000000+00:00
  FILES: 1 added
    added   /etc/systemd/system/vm-save.timer.d/override.conf
  PACKAGES: 2 changed
    changed libssl3  "3.0.13-0" → "3.0.13-1"
    changed openvswitch-switch  "3.3.0-1" → "3.3.0-2"
  SERVICES: 1 changed
...
INBOUND HTTP REQUESTS
  1427 requests · 18 server errors · 23 ≥1s (when timing is logged)
  Responses: 200: 1394, 404: 15, 503: 18
...
EVENT TIMELINE
  2026-09-21T11:13:14.000000+00:00 [network] br-ex: Lost carrier
  2026-09-21T11:13:30.000000+00:00 [services] vm-save.service: Failed with result 'exit-code'.
...
COVERAGE
  • DEMO: synthetic events and counts. No real host data was collected.
  • HTTP counts describe retained access logs, not every network connection.
```

The event window and state-comparison baseline are shown separately. In live reports,
unreadable sources and missing baselines are reported under coverage; the demo above
shows the retained-log caveat rather than simulating a permission failure. Temporal
correlation does not establish that an update caused an incident.

## When to use it

| Situation | Workflow |
| --- | --- |
| Returning over SSH | Read the briefing since your previous tracked session, then inspect details |
| Reviewing a deployment | Save a baseline, deploy, then compare state and review recent events |
| Running an operations agent | Read JSON, process the evidence, then acknowledge successful processing |

## Quick start

On a Linux host with Git and Python 3.10+ installed:

```bash
git clone https://github.com/haramj/hostdelta.git
cd hostdelta
export PATH="$PWD/bin:$PATH"
hostdelta init
hostdelta brief --since 24h
```

`init` establishes the first baseline. State changes become available as later
snapshots are saved; retained logs may already provide recent events. Standard
Nginx/Apache access logs are discovered at initialization when present. Missing
permissions or logs appear as coverage gaps.

To preview the output without initializing or reading a host, run
`./bin/hostdelta demo`. For ongoing evidence retention and collection while nobody
is logged in, follow the [continuous collection guide](docs/collection.md).

## Where HostDelta fits

Terraform provisions infrastructure. Ansible configures hosts. Virtualization
platforms manage VM lifecycles. HostDelta summarizes observed changes inside a Linux
host or guest VM, helping the next operator recover recent operational context.

These tools can be composed through CLI workflows today: capture a HostDelta baseline,
run your existing deployment or configuration workflow, then review the evidence.

**Integration scope in v0.2.0:** optional OpenStack and Proxmox API adapters are
implemented for selected health and inventory observations. They do not provision
resources or manage VM lifecycles. Native libvirt and VMware integrations are not
included. The adapter scope and authentication requirements are documented
[separately](docs/adapters.md); live deployment validation is still required.

## Three workflows

The examples below assume HostDelta is initialized and available on your PATH.

### Return over SSH

```bash
hostdelta hook bash                 # Print the opt-in hook; use zsh for Zsh
# Review the hook and add it to your shell configuration on the Linux host.
# On a later SSH session:
hostdelta brief --since last-login
hostdelta brief --full
```

The hook displays a time-limited briefing in interactive SSH sessions. `last-login`
means the previous session tracked by HostDelta. Until that history exists, the
briefing explicitly falls back to the past 24 hours.

### Review a deployment

```bash
# Save the observed host state before deployment.
hostdelta snapshot --label before-deploy

# Run your deployment or configuration workflow.

# Capture the observed state after deployment, then compare saved snapshots.
hostdelta snapshot --label after-deploy
hostdelta diff before-deploy after-deploy

# Review recent events and HTTP request summaries.
hostdelta brief --since 30m
```

A HostDelta snapshot records observed host state; it is not a VM snapshot or a
restore point. Use fresh labels for each deployment. The event window should cover
your deployment; a diff alone cannot reveal a transient failure that recovered.
To practice this flow without collecting host data or deploying anything, run the
[synthetic deployment review walkthrough](examples/deployment_review.py).

### Give an operations agent context

```bash
hostdelta brief --consumer ops-agent --json
# Process the report and persist the result.
# Set REPORT_UNTIL to that successfully processed report's window.until value.
hostdelta ack --consumer ops-agent --until "$REPORT_UNTIL" --json
```

Reading does not advance the consumer's cursor. Each consumer is independent;
acknowledge only after successful processing. Add `--archive` when using the
continuous collector's retained evidence. See the [agent contract](docs/agents.md)
for retries, alert exit codes and handling untrusted log content.

## What it collects

| Evidence | Sources and scope |
| --- | --- |
| State differences | Installed packages, systemd services, interfaces/routes, watched file hashes, kernel and boot ID |
| System events | Journal records for service failures, SSH authentication, sudo, OOM and selected network/firewall warnings |
| HTTP requests | Nginx/Apache access logs: request counts, paths, peers, response codes and timing when logged |
| Application logs | Structured JSONL with service, event, severity, trace/span and run context; optional Python logging SDK |
| Health and restarts | Configured service/API observations, systemd invocation changes and sampled failure/recovery intervals |
| Optional integrations | OpenStack endpoint probes; Proxmox inventory/quorum; TCP sampling or conntrack subscriptions |

HostDelta reads evidence; it does not repair services or modify cloud resources.
For application instrumentation, see the [logging guide](docs/application-logging.md),
[JSON Schema](schemas/application-log.schema.json) and
[runnable example](examples/structured_app.py).

## Installation

Build and install the portable CLI from the source checkout on your Linux server:

```bash
python3 scripts/build_zipapp.py
mkdir -p ~/.local/bin
install -m 755 dist/hostdelta.pyz ~/.local/bin/hostdelta
export PATH="$HOME/.local/bin:$PATH"
hostdelta --version
hostdelta init
hostdelta doctor
```

The portable build uses only Python's standard library. If using supplied release
artifacts, verify them against their `SHA256SUMS` before installation. A wheel can
also be installed in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./hostdelta-0.2.0-py3-none-any.whl
.venv/bin/hostdelta --version
```

`pip install .` is supported and needs setuptools during the build. Use the project's
own source or artifacts; a matching package name on PyPI does not establish provenance.

## Limitations and readiness

- **Beta, not deployment certification.** The [initial CI run](https://github.com/haramj/hostdelta/actions/runs/35689379982) passed tests and packaging on Linux and macOS with Python 3.10, 3.12, and 3.14, including Linux host smoke checks. Real service lifecycle and OpenStack/Proxmox acceptance remains to be completed in the target environment.
- **Linux collection.** Live host collection targets systemd Linux. macOS supports the demo, application instrumentation, archive tools and tests, not live host snapshots.
- **Incomplete evidence stays visible.** Permissions, missing logs, rotation, retention and read limits can leave gaps. Unobserved activity cannot be reconstructed, and a clean report does not prove a healthy host.
- **Observed state, not restoration.** Snapshots are not atomic machine images. Changes reverted between observations can be missed. Restart counts and outage intervals are bounded by the evidence and sampling frequency.
- **Retention is implemented in v0.2.0.** The daemon defaults to 30-day age-based cleanup; manual cleanup uses `prune`. It can remove unacknowledged events. Named snapshots and open incidents remain, so disk usage still needs monitoring.
- **Sensitive and privileged sources need care.** Redaction is best effort. TCP conntrack is optional and requires separately granted privileges. HostDelta does not capture every packet or provide a tamper-proof audit trail.

Use the [live-validation runbook](docs/live-validation.md) and its read-only verifier
before rollout. Review [security](SECURITY.md) and [operations](docs/operations.md)
for source permissions, private state, backups and upgrades.

## Documentation

| Guide | Purpose |
| --- | --- |
| [CLI reference](docs/cli-reference.md) | Commands, timestamps, configuration scope and exit codes |
| [Continuous collection](docs/collection.md) | Daemon setup, archive queries, restart/outage semantics and TCP modes |
| [Operations](docs/operations.md) | Service identities, credentials, retention, backup and upgrades |
| [Application logging](docs/application-logging.md) | JSON format, instrumentation, correlation and redaction |
| [Adapters](docs/adapters.md) | Exact OpenStack/Proxmox API scope and failure interpretation |
| [Agent contract](docs/agents.md) | JSON, consumer cursors, acknowledgement and retries |
| [Architecture](docs/architecture.md) | Storage, source limits, checkpoint recovery and migrations |
| [Live validation](docs/live-validation.md) | Target-environment acceptance and isolated failure tests |

## Contributions welcome

Help shape HostDelta through code, documentation, bug reports, and real-world
Linux, OpenStack, or Proxmox testing. You do not need to contribute code to make
a difference. First-time open-source contributors are welcome.

- Browse the [contributor roadmap](docs/contributor-roadmap.md) for scoped work and dependencies.
- Start with [good first issues](https://github.com/haramj/hostdelta/labels/good%20first%20issue).
- Explore [help wanted](https://github.com/haramj/hostdelta/labels/help%20wanted) for testing and larger tasks.
- Ask questions, share ideas, or describe your setup in [Discussions](https://github.com/haramj/hostdelta/discussions).
- Read [CONTRIBUTING.md](CONTRIBUTING.md) and our [Code of Conduct](CODE_OF_CONDUCT.md).

Review logs and reports for sensitive data before sharing. Report vulnerabilities
through [private security reporting](https://github.com/haramj/hostdelta/security/advisories/new).

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
```

Integration tests need permission to bind temporary localhost ports and run child
processes. See [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md) and
the [release checklist](docs/releasing.md).

## Roadmap

Near-term priorities are target-environment acceptance, fixes informed by operator
feedback, and clearer evidence for incomplete observations. Additional platform
adapters and broader distribution validation are potential future work, not current
support commitments.
