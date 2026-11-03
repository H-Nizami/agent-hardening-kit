# Pinned versions

Looked up on 2026-10-22. GitHub Actions are pinned to the full commit SHA the release tag resolves to (`gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`). Binaries are pinned to an exact version and verified against the sha256 in the project's published checksums file (see `scripts/tools.sh`).

## GitHub Actions

| Action | Tag | Commit SHA | Looked up |
|---|---|---|---|
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | 2026-10-22 |
| `actions/setup-python` | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` | 2026-10-22 |
| `actions/upload-artifact` | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` | 2026-10-22 |

## Binaries (`scripts/tools.sh`)

| Tool | Version | Checksums file |
|---|---|---|
| d2 | v0.9.0 | https://github.com/terrastruct/d2/releases/download/v0.9.0/SHA256SUMS |
| actionlint | 1.7.12 | https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_checksums.txt |
| gitleaks | 8.30.1 | https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt |
| trivy | 0.76.1 | https://github.com/aquasecurity/trivy/releases/download/v0.76.1/trivy_0.76.1_checksums.txt |
| temporal CLI | 1.6.1 | https://github.com/temporalio/cli/releases/tag/v1.6.1 (checksums asset) |
| otelcol-contrib | 0.164.0 | https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.164.0 (checksums asset) |
| promtool | 3.15.1 | https://github.com/prometheus/prometheus/releases/download/v3.15.1/sha256sums.txt |
| kind | 0.33.0 | https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0 (per-binary `.sha256sum`) |
| kubectl | 1.34.11 | https://dl.k8s.io/release/v1.34.11/bin/<os>/<arch>/kubectl.sha256 |
| helm | 3.22.1 | https://get.helm.sh/helm-v3.22.1-<os>-<arch>.tar.gz.sha256sum (looked up 2026-11-02) |

Sums are hardcoded for `darwin-arm64`, `darwin-amd64`, `linux-amd64` and `linux-arm64`. Other platforms fail with a clear error.

## Python application (T1)

Python 3.12 is required. Direct runtime pins are `langgraph==1.2.16` and
`pydantic==2.14.1`; test/build pins are `pytest==9.1.1` and `setuptools==84.2.1`.
`uv.lock` records transitive versions and published distribution hashes.
`requirements-dev.lock` is the hash-bearing pip export used by CI and quickstart.
The package is installed with `--no-build-isolation --no-deps` after those locked
dependencies, so the build backend cannot resolve additional unpinned packages.

Regenerate after an intentional dependency change:

```bash
uv lock
uv export --locked --extra dev --no-emit-project --output-file requirements-dev.lock
```

The application uses a local fixture backend; no provider key or model service is
required. Installed distribution resources include the golden cases and corpus.

## Container images

Base and service images are pinned by tag and multi-platform index digest in
`Dockerfile`, `deploy/kind.yaml` and `deploy/chart/values.yaml`. The table with
lookup dates is in [deploy/README.md](../deploy/README.md#pinned-base-images).
`scripts/check_manifests.py` rejects any rendered image without a digest, except
the locally built worker image that the kind driver loads by its exact build tag.
