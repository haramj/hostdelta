# Contributing to HostDelta

HostDelta is built around recoverable local evidence, explicit uncertainty and a
small dependency footprint. Contributions should improve operational usefulness
without claiming observations the collectors cannot establish.

See the [contributor roadmap](docs/contributor-roadmap.md) for concrete starting
points, acceptance criteria and dependencies. Check linked PRs and comments before
starting so contributors can avoid duplicating work.

## Your first contribution

Documentation, reproducible bug reports, sanitized test fixtures, and real-world
validation reports are as valuable as code. Browse the repository's `good first issue`
label for bounded tasks and `help wanted` for work that needs community input.
Comment on an issue if you want to work on it; maintainers can help clarify scope.
For small corrections, a focused pull request is welcome without opening an issue first.

Fork the repository, clone your fork, create a branch, and follow the setup below.
Open a pull request against `main` and describe what you checked. Draft pull requests
are welcome for early feedback. Ask usage questions and propose ideas in
[Discussions](https://github.com/haramj/hostdelta/discussions).

For environment validation, follow [the runbook](docs/live-validation.md) and report
versions, enabled sources, observed results, and gaps. Do not publish raw state
databases, credentials, private endpoints, or unsanitized production logs.

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
