# Contributing to HostDelta

HostDelta is built around recoverable local evidence, explicit uncertainty and a
small dependency footprint. Contributions should improve operational usefulness
without claiming observations the collectors cannot establish.

See the [contributor roadmap](docs/contributor-roadmap.md) for concrete starting
points, acceptance criteria and dependencies. Check linked PRs and comments before
starting so contributors can avoid duplicating work.

## Your first contribution

### First-Contribution Command Walkthrough

Follow these steps to set up the project, test changes locally, and open a PR:

1. **Fork and Clone the Repository:**
   ```bash
   git clone [https://github.com/](https://github.com/)<YOUR-USERNAME>/hostdelta.git
   cd hostdelta
   git remote add upstream [https://github.com/hostdelta/hostdelta.git](https://github.com/hostdelta/hostdelta.git)
   git fetch upstream
   git checkout -b my-feature-branch upstream/main

## Development setup

Use Python 3.10 or newer. Runtime code uses the standard library only.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
./bin/hostdelta config --check examples/hostdelta.json
```

The suite starts temporary loopback HTTP servers and child collector processes.
Run it in an environment that permits those operations. Never point tests at a live
customer database or inject failures into production services. Live acceptance has
its own runbook in `docs/live-validation.md`.

## Submitting a change

Open an issue for a substantial new collector or semantic change. Describe the
operator problem, source contract, permissions, failure modes and validation plan.
For a pull request, include the user-visible behavior, relevant tests and operational
limitations. Keep changes focused and maintainers' review workload reasonable.

New collectors must:

- Bound time, memory/output and per-cycle work; make backlog or loss visible.
- Preserve unavailable/partial source status rather than inventing empty state.
- Commit event evidence and its source checkpoint atomically.
- Retain stable evidence identifiers and avoid inferring causation from proximity.
- Avoid `shell=True`, implicit privilege escalation and host/cloud remediation.
- Avoid raw secrets, request bodies, arbitrary command arguments and terminal controls.
- Document platform assumptions, schema changes and upgrade/rollback implications.

Keep schema-version-1 CLI output backward compatible by adding fields rather than
renaming/removing them. Database schema changes require migration tests. Prefer
meaningful tests for failure boundaries, retries, rotation, permissions and recovery
over tests that only mirror helper implementation.

## Project conduct

Follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Be constructive and respectful. Critique technical decisions rather than people.
Harassment, discriminatory remarks and publication of others' private information
are not acceptable. Maintainers may moderate contributions that violate these rules.

By contributing, you agree that your contribution is available under the repository's
MIT license. Do not contribute code, logs or fixtures you do not have permission to share.
