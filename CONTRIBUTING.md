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

### First-Contribution Command Walkthrough

Follow these steps to set up the project, test changes locally, and open a pull request.

#### 1. Fork and clone the repository

1. Open the [HostDelta repository](https://github.com/haramj/hostdelta).
2. Click **Fork** to create your own copy.
3. Replace `YOUR_GITHUB_USERNAME` below with your GitHub username.

Run these commands in a Bash terminal:

```bash
export GITHUB_USER="YOUR_GITHUB_USERNAME"

git clone "https://github.com/$GITHUB_USER/hostdelta.git"
cd hostdelta

git remote add upstream https://github.com/haramj/hostdelta.git
git remote -v
```

The `origin` remote should point to your fork, and `upstream` should point to
the original HostDelta repository.

#### 2. Create a branch

Create a separate branch for your contribution:

```bash
git switch -c my-feature-branch
```

Use a short, descriptive branch name related to your changes.

#### 3. Make and review your changes

Make your changes, then check which files were modified:

```bash
git status
```

Review your changes:

```bash
git diff
```

Check for whitespace errors:

```bash
git diff --check
```

Fix any reported errors before continuing.

#### 4. Set up the development environment

Use Python 3.10 or newer. HostDelta runtime code uses the Python standard library
only. Runtime dependencies should remain empty.

Run the following commands from the repository root.

Run the automated test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Build the release:

```bash
python3 scripts/build_release.py
```

Check the example configuration:

```bash
./bin/hostdelta config --check examples/hostdelta.json
```

#### 5. Run optional development tools

Ruff and Bandit are optional development tools. They are not runtime dependencies.
Install them in an isolated virtual environment.

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the pinned versions:

```bash
python -m pip install ruff==0.16.9 bandit==1.9.4
```

Run Ruff:

```bash
ruff check .
```

Run Bandit:

```bash
bandit -c pyproject.toml -r src scripts
```

These tools are intended for development checks only. Do not add them to
the project's runtime dependencies.

#### 6. Stage and commit your changes

Check your working tree:

```bash
git status
```

Stage only the files you intend to include. For example:

```bash
git add CONTRIBUTING.md
```

Replace the filename with the files you actually changed.

Review the staged changes:

```bash
git diff --cached
```

Commit your changes:

```bash
git commit -m "Describe your contribution"
```

#### 7. Push your branch to your fork

Push your branch to GitHub:

```bash
git push -u origin my-feature-branch
```

Replace `my-feature-branch` with your actual branch name.

#### 8. Open a pull request

1. Open your fork of HostDelta on GitHub.
2. Select the branch containing your changes.
3. Click **Compare & pull request**.
4. Set the base repository to `haramj/hostdelta`.
5. Set the base branch to `main`.
6. Add a clear title and description explaining your changes.
7. Include the validation commands you actually ran and their results.
8. Submit your pull request.

Draft pull requests are welcome when you want early feedback before your work
is ready for merging.

For a contribution that fully addresses an issue, include `Closes #ISSUE_NUMBER`
in the pull request description. GitHub will close the linked issue when the
pull request is merged.

You can also ask usage questions and propose ideas in
[Discussions](https://github.com/haramj/hostdelta/discussions).

## Development setup

Use Python 3.10 or newer. Runtime code uses the standard library only.

Run the following commands from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
./bin/hostdelta config --check examples/hostdelta.json
git diff --check
```

The suite starts temporary loopback HTTP servers and child collector processes.
Run it in an environment that permits those operations. Never point tests at a live
customer database or inject failures into production services.

Ruff and Bandit are optional development tools. Their commands and installation
instructions are provided in the First-Contribution Command Walkthrough.

Do not claim that a check passed unless you actually ran it. If your environment
does not support a check, mention that clearly in your pull request and explain
what you were unable to run.

Live acceptance has its own runbook in
[docs/live-validation.md](docs/live-validation.md). A documentation-only change
does not require access to a live cloud environment.

## Submitting a change

Open an issue for a substantial new collector or semantic change. Describe the
operator problem, source contract, permissions, failure modes and validation plan.

For a pull request, include the user-visible behavior, relevant tests and
operational limitations. Report the validation commands you actually ran and
identify any checks you could not perform.

Keep changes focused and maintainers' review workload reasonable.

Documentation improvements, reproducible bug reports, sanitized test fixtures
and real-world validation reports are also welcome contributions.

External workflow approval, when required by GitHub or a CI provider, is
separate from code review and approval by the project maintainers.

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

Do not publish raw state databases, credentials, private endpoints or unsanitized
production logs in issues, pull requests, test fixtures or documentation.

## Project conduct

Follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Be constructive and respectful. Critique technical decisions rather than people.
Harassment, discriminatory remarks and publication of others' private information
are not acceptable. Maintainers may moderate contributions that violate these rules.

By contributing, you agree that your contribution is available under the repository's
MIT license. Do not contribute code, logs or fixtures you do not have permission to share.
