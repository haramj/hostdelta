# HostDelta contributor roadmap

HostDelta helps operators catch up on observed host activity. Contributions are
welcome in documentation, tests, application integration and local collector
reliability. Pick one bounded issue; there is no need to implement this whole list.

This is a community backlog, not a release schedule or a claim that the proposed
features already exist. Linked issues and PRs are the source of truth for availability.
Check their latest comments before starting; another contributor may already be working.

## First contributions

- [test: Run the stdlib logging example in the test suite](https://github.com/haramj/hostdelta/issues/8)
- [test: Cover corrupt and bounded gzip HTTP log scans](https://github.com/haramj/hostdelta/issues/10)
- [docs: Add a reviewed logrotate recipe for application JSONL](https://github.com/haramj/hostdelta/issues/11)
- [test: Cover IPv6 and original-direction conntrack tuples](https://github.com/haramj/hostdelta/issues/16)
- [docs: Add an executable deployment review walkthrough](https://github.com/haramj/hostdelta/issues/20)

## Application and agent workflows

- [examples: Demonstrate isolated asyncio logging contexts](https://github.com/haramj/hostdelta/issues/9)
- [examples: Bound agent polling and preserve cursors on failure](https://github.com/haramj/hostdelta/issues/12)
- [http: Add opt-in mappings for structured access-log fields](https://github.com/haramj/hostdelta/issues/15)

## Reliability and operations

- [fix: Close HTTP error responses in cloud adapter probes](https://github.com/haramj/hostdelta/issues/21)
- [brief: Align archive coverage checks with configured source readiness](https://github.com/haramj/hostdelta/issues/19)
- [cli: Add a consistent private SQLite backup command](https://github.com/haramj/hostdelta/issues/13)
- [perf: Add a reproducible archive workload benchmark](https://github.com/haramj/hostdelta/issues/14)
- [build: Make portable release artifacts reproducible](https://github.com/haramj/hostdelta/issues/17)

## Real-environment validation

- [validation: Build a distro-specific Linux collector evidence matrix](https://github.com/haramj/hostdelta/issues/18)

## Existing opportunities

- [Evidence and coverage glossary](https://github.com/haramj/hostdelta/issues/1)
- [Non-UTC combined access-log tests](https://github.com/haramj/hostdelta/issues/2)
- [OpenStack target-environment acceptance](https://github.com/haramj/hostdelta/issues/4)
- [Proxmox target-environment acceptance](https://github.com/haramj/hostdelta/issues/5)

## Getting started

1. Read the issue's scope, current comments and linked PRs. Comment on the issue if you want to take it on.
2. Read [CONTRIBUTING.md](https://github.com/haramj/hostdelta/blob/main/CONTRIBUTING.md), then open a focused draft PR early.
3. Include the checks you ran and the checks you could not run. Use synthetic or sanitized evidence.

Beginner labels indicate bounded scope, not a deadline. Larger API changes should
start with a short contract proposal. In particular, archive coverage work (#19)
should reuse the source-readiness helper after that PR is available; backup (#13)
and structured HTTP mappings (#15) need their CLI/configuration contracts agreed first.

Real OpenStack/Proxmox reports remain separate from mocked tests. Do not upload raw
state databases, credentials or private production logs. Maintainers review PRs and
approve eligible external CI runs; CI approval is not code approval.
