# Contributing

The kit takes a public LangGraph multi-agent RAG demo from "works in a demo" to
production: evals, guardrails, tracing, budgets, Temporal durability, approvals
and SLOs. Design notes are in [`docs/design.md`](docs/design.md); decisions are
recorded as ADRs in [`docs/decisions/`](docs/decisions/).

## Setup

Install dependencies from `requirements-dev.lock` with hash verification, then
install this package. `make tools` installs the pinned binaries into `.bin/`.

## Commands

- `make tools`: install pinned binaries into `.bin/`.
- `make lint`: actionlint, shellcheck, `d2 validate`, hygiene checks.
- `make test`: unit/regression tests plus hygiene checks; uses `.venv` when present.
- `make eval`: all 39 fixed offline cases, with missing-case and regression gates.
- `make demo`: a tenant-scoped query using the deterministic fixture backend.
- `make diagram`: render `docs/img/architecture.{svg,png}` (PNG 2400–4000 px wide, target 3000).
- `make chart`: helm lint/template plus rendered-manifest policy check (no cluster).
- `make chaos`: owned kind cluster pod-kill proof; writes `evidence/cluster/`, always deletes its cluster.
- `make up` / `make down`: not used; see [`docs/runbook.md`](docs/runbook.md) for start/approve/reject/recover/teardown.

## Pinned versions

| Tool | Version | Where pinned |
|---|---|---|
| d2 | v0.9.0 | `scripts/tools.sh` |
| actionlint | 1.7.12 | `scripts/tools.sh` |
| gitleaks | 8.30.1 | `scripts/tools.sh`, `security.yml` |
| trivy | v0.76.1 | `scripts/tools.sh`, `security.yml` |
| temporal CLI, otelcol-contrib, promtool | 1.6.1, 0.164.0, 3.15.1 | `scripts/tools.sh` |
| kind, kubectl, helm | 0.33.0, 1.34.11, 3.22.1 | `scripts/tools.sh` |
| Container images | tag + index digest | `Dockerfile`, `deploy/kind.yaml`, `deploy/chart/values.yaml` |
| Python (CI) | 3.12 | `.github/workflows/ci.yml` |
| GitHub Actions | commit SHAs | workflow `uses:` lines; table in [`docs/pins.md`](docs/pins.md) |

Pin every version. Check the upstream docs and `--help` output before upgrading a
tool or relying on a new flag.

## Conventions

- No secrets in the repo. Use placeholders only (for example account `000000000000`, `example.com`).
- No employer references. `scripts/check-hygiene.sh` and `scripts/check-author.sh` enforce the commit and content policy.
- Numbers in the README and diagram come from checked-in evidence. Unmeasured numbers are marked `Not yet measured: …`.
- Every GitHub Action is pinned to a full commit SHA with a `# vX.Y.Z` comment.
- Workflows use least-privilege `permissions:` (top level `{}`, each job grants only what it needs).
- The "before" baseline is someone else's public demo: always credit it by name and link.

## Layout

- `docs/`: architecture diagram source, icons, rendered images, design notes, ADRs, pins.
- `scripts/`: diagram rendering, tool installer, hygiene and author checks.
- `.github/workflows/`: CI, security scans, author check.
- `src/agent_hardening_kit/`: graph, local backend, contracts, admission and evaluation.
- `tests/`: unit tests and deliberate-regression checks.
- `eval/results/latest.*`: reproducible offline evidence. `eval/results/live-*`: a dated live-model run, with its limits stated in the report.
- `evidence/`: local Temporal recovery, Collector roundtrip and kind pod-kill reports.
- `deploy/`: Helm chart and kind config for the local lab.

## Definition of done

`make lint test eval diagram` is green and `actionlint` is clean. Runtime,
telemetry and cluster evidence live in `evidence/`; regenerate it rather than
hand-editing it. Live-model results cover the fixtures only; production task success remains unmeasured.
