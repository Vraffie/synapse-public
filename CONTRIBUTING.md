# Contributing

Synapse is an early-stage project and is **not accepting outside code contributions yet**. Issues and
discussion are welcome; pull requests may be closed without review while the architecture and the trust model are
still settling. Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## If you want to try it

The demo runs entirely on the built-in mock provider, with no real agents and no quota:

```bash
uv run --extra desktop --with-editable . python scripts/demo.py
```

See the [README](README.md) and [install](docs/operations/install.md). Real provider execution is opt-in and
carries the same-user trust limits described in the [security review](docs/security/review-2026-11-20.md).

## Project rules (for maintainers and agents)

- Read `AGENTS.md`, `docs/architecture/ARCHITECTURE.md` and `IMPLEMENTATION_BACKLOG.md` first. Propose
  architecture changes through an ADR.
- Every change needs tests, migration compatibility, failure modes and updated documentation.
- Security-sensitive and deployment code needs an independent review before merge.
- Run `scripts/check.sh` (or the commands in [ci and local checks](docs/operations/ci-and-local-checks.md)) before
  opening a pull request.

## Licence

By contributing you agree that your contribution is licensed under the MIT License (see `LICENSE`).
