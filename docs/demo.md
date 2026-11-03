# Short demonstration

Everything below is free and local. No model provider, cloud account or
credential is used. Allow about five minutes for parts 1 and 2; part 3 builds an
image and a cluster and takes longer.

## 1. Offline controls (no Docker)

```bash
make demo        # one tenant-scoped answer with a tenant-visible citation
make eval        # 39 fixed cases; fails on any regression or missing case
```

Open [the fixture report](../eval/results/latest.md). The unsafe baseline sends
foreign-tenant documents to its backend in 31 cases; the hardened path sends none.

## 2. Durable approval on localhost

Follow the [runbook](runbook.md#localhost-stack): start the Temporal dev server,
the worker and the approval controller, then:

```bash
.venv/bin/python -m agent_hardening_kit start --tenant acme --side-effect
.venv/bin/python -m agent_hardening_kit status --workflow-id <id>   # pending_approval
```

Kill the worker with `kill -9`, start it again, and query `status`: the run is
still pending and the generation step does not run again. Then approve it:

```bash
.venv/bin/python -m agent_hardening_kit decide --workflow-id <id> --tenant acme --decision approve
.venv/bin/python -m agent_hardening_kit result --workflow-id <id>
```

The result shows one synthetic ticket. [Local recovery evidence](../evidence/runtime/latest.md)
records the automated version of this run, including reject, duplicate and expiry cases.

## 3. Pod kill on a disposable kind cluster

```bash
make chaos
```

The driver creates its own `ahk-chaos-<id>` cluster, installs the chart, deletes
the worker pod while three runs wait for approval, approves them after the
replacement is Ready, checks effects, replays histories, checks telemetry for
canaries and deletes the cluster. Read the result in
[evidence/cluster/latest.md](../evidence/cluster/latest.md), recorded by the CI
`kind-chaos` job ([run](https://github.com/H-Nizami/agent-hardening-kit/actions/runs/41627236161)). Run `make chaos` to repeat it locally.
