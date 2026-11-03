# Local deployment

`chart/` deploys one fixture worker with an OTel Collector sidecar, a Temporal
**development** server, Prometheus, and a provisioned Grafana dashboard. It is a
single-host local lab. It does not deploy a production Temporal cluster, external
identity provider, Langfuse or Tempo. No live model endpoint is used.

The worker and Temporal each use a 1 GiB ReadWriteOnce PVC. Both deployments use
one replica and Recreate rollout. The worker SQLite budget/effect store must not
be scaled across hosts; completed Temporal Activities are recorded in the
Temporal dev server's separate SQLite database. Worker pod deletion preserves
those PVCs. Collector trace files and Prometheus/Grafana data are intentionally
ephemeral; capture evidence before teardown.

All containers run as non-root UID/GID 10001 with RuntimeDefault seccomp,
capabilities dropped, privilege escalation disabled, a read-only root filesystem,
and explicit writable volumes. Service-account token mounting is disabled. The
worker readiness probe requires its polling-started marker and a reachable
Temporal socket. Liveness checks process presence, not model/provider quality.
Resources are bounded for a small local Docker environment. The recorded
kind run had 2 CPUs and 7.75 GiB of memory (`docker_resources` in the evidence). No Service is exposed
outside the cluster; host inspection uses loopback-bound port forwarding.

Grafana has anonymous **Viewer** access with login/signup and initial admin
creation disabled. This avoids shipping a demo admin password. Treat its contents
as local synthetic data, not an authenticated production observability service.
NetworkPolicy manifests permit DNS, worker-to-Temporal, Prometheus-to-Collector
scrapes and Grafana-to-Prometheus queries. Default kindnet does not implement
NetworkPolicy enforcement: these rules express policy intent in that environment.
Use a policy-capable CNI and separate verification before claiming enforcement.

## Rendering and image build

```bash
make chart     # pinned helm 3.22.1: lint --strict, template, rendered-manifest policy check
docker build --tag ahk-worker:local .
```

`scripts/check_manifests.py` fails on non-ClusterIP Services, node ports, host
namespaces or hostPath volumes, mounted service-account tokens, root or
privilege-escalating containers, writable root filesystems, missing dropped
capabilities, missing CPU/memory requests or limits, and undigested images.
`make chaos` also runs a strict server-side dry run against its own cluster.

The Temporal image has no passwd entry for UID 10001, so the chart sets `USER`
for the server's user lookup. Without it the server exits at start-up.

The chart's local worker image uses `Never` pull policy. The owned-cluster driver
loads the image into kind and passes its exact build tag. Registry deployments
can supply `image.digest` and `image.pullPolicy=IfNotPresent`. No external chart
dependencies or install-time downloads are present.

`kind.yaml` pins Kubernetes 1.34.11 from the kind v0.33.0 release. Runtime scripts
must create a unique owned cluster name, pass its explicit context and namespace
on every command, and remove only that cluster. Never install this local lab into
an existing/current remote context. `scripts/check_cluster.py` (`make chaos`)
does this with its own kubeconfig file and writes [evidence](../evidence/cluster/latest.md);
the [runbook](../docs/runbook.md) describes each step.

## Pinned base images

These multi-platform index digests were read from the registries on 2026-10-29:

| Image | Index digest |
|---|---|
| Python 3.12.16 slim-bookworm | `sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337` |
| Temporal CLI/dev server 1.6.1 | `sha256:0936c35553b2d8d13c368e44f2628d4d12d4d53d45046f0673c4726499e9a3c0` |
| OTel Collector contrib 0.164.0 | `sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b` |
| Prometheus v3.15.1 | `sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2` |
| Grafana 13.3.0 | `sha256:488984b716c0862639f91a698adbe2862677a5a07bad42da5f03f3030883b205` |

The Docker build installs every application/build dependency using the committed
hash-locked requirements, then installs the package without dependency resolution
or build isolation. The local lab image intentionally retains locked test tools.
The runtime UID has no write access to the installed application.

Source configuration is in `observability/` at the repository root. The chart
packages exact copies in `chart/files/`; after an intentional source change keep
the Collector config, SLO rules and dashboard copies synchronized. Do not add
secrets or environment-specific credentials to either source or packaged files.
