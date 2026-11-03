# Operator runbook

Two environments are supported: a localhost process stack for daily use, and a
disposable kind cluster for the pod-kill proof. Both use the free deterministic
fixture backend. Neither one calls a model provider or a cloud service.

Install the dependencies first (see the README quickstart) and the pinned
binaries with `scripts/tools.sh temporal kind kubectl helm`. The kind path also
needs Docker. The localhost commands below were run end to end on 2026-11-02,
including a `kill -9` of the worker while a run waited for approval.

## Localhost stack

Use four terminals in the repository root. Every command binds to `127.0.0.1`.

### Start

```bash
# 1. Temporal development server (state in .cache/runtime/temporal.sqlite)
mkdir -p .cache/runtime
.bin/temporal --disable-config-file --disable-config-env server start-dev \
  --ip 127.0.0.1 --port 17333 --headless --db-filename .cache/runtime/temporal.sqlite

# 2. Worker (SQLite budget/effect store in .cache/runtime/agent.sqlite)
.venv/bin/python -m agent_hardening_kit worker

# 3. Approval controller (Unix socket in .cache/control/, owner-only)
.venv/bin/python -m agent_hardening_kit approval-controller

# 4. Start a run that proposes a synthetic support ticket
.venv/bin/python -m agent_hardening_kit start --tenant acme --side-effect
```

`start` prints the workflow ID. The run stops at `pending_approval` after the
answer is generated, validated and settled.

### Inspect

```bash
.venv/bin/python -m agent_hardening_kit status --workflow-id <id>
```

`stage` shows the current step. `approval_state` is `pending`, `approved`,
`rejected` or `expired`. The default approval deadline is 300 seconds
(`--approval-ttl-seconds`). After the deadline the run completes with no effect.

### Approve or reject

```bash
.venv/bin/python -m agent_hardening_kit decide --workflow-id <id> --tenant acme --decision approve
.venv/bin/python -m agent_hardening_kit decide --workflow-id <id> --tenant acme --decision reject
.venv/bin/python -m agent_hardening_kit result --workflow-id <id>
```

The controller accepts a decision only from the same local user ID (checked with
the kernel peer credential on the socket). It maps that user to the
`local-operator` approver. The workflow then checks tenant, action ID, approver
and deadline again. A second, different decision for the same action is refused.
An approved run creates exactly one synthetic ticket; a rejected or expired run
creates none.

Security limit: anyone who can reach the Temporal port directly has an
infrastructure role. The approval policy does not protect an unauthenticated
Temporal endpoint from a hostile local user.

### Recover from a worker crash

Kill the worker (terminal 2, or `kill -9`) and start it again with the same
command. Pending runs stay in Temporal. Completed Activities are not repeated,
and the run continues from where it stopped. `make` does not restart it for you.

To re-run the automated local proof, which kills the worker with SIGKILL:

```bash
.venv/bin/python scripts/check_runtime.py --out .cache/runtime-proof
```

### Tear down

Stop terminals 1 to 3 with Ctrl-C. Then remove local state:

```bash
rm -rf .cache/runtime .cache/control
```

## Disposable kind cluster

```bash
make chart   # helm lint/template + rendered-manifest policy check, no cluster
make chaos   # full proof, writes evidence/cluster/latest.{json,md}
```

`make chaos` runs `scripts/check_cluster.py`. It does these steps in order:

1. Refuse to start if a cluster with the generated name `ahk-chaos-<id>` exists.
2. Build the worker image and create the cluster with its own kubeconfig in
   `.cache/cluster-evidence/<id>/`. It never reads or changes `~/.kube/config`.
3. Load the image, run the policy check and a strict server-side dry run, then
   `helm install` with `--wait`.
4. Check requests/limits on every container, bound PVCs, a writable `/data`
   mount and ClusterIP-only Services.
5. Start three runs that need approval. Wait until each one is at
   `pending_approval`, force-delete only the worker pod, and wait for the
   replacement to be Ready.
6. Approve the three runs, then exercise reject, duplicate-decision and expiry
   cases. Save and replay every history.
7. Send a run with canary strings and check traces and Prometheus series from
   the replacement pod for the canaries.
8. Uninstall the release, delete the cluster and check that its name is not in
   `kind get clusters`. This step runs on success, failure and Ctrl-C/SIGTERM.

Each command uses `--kubeconfig <owned file> --context kind-ahk-chaos-<id>
--namespace ahk`. A failed or interrupted run still writes evidence with
`status: failed` or `interrupted` and the failed stage.

### Manual inspection during a run

Use the owned kubeconfig and context printed in
`.cache/cluster-evidence/<id>/commands.log`, for example:

```bash
.bin/kubectl --kubeconfig .cache/cluster-evidence/<id>/kubeconfig \
  --context kind-ahk-chaos-<id> --namespace ahk get pods
```

Host access is through loopback-bound port forwards only. No Service is exposed
outside the cluster.

### Leftover cluster

If the driver itself was killed with SIGKILL, list clusters and delete only the
`ahk-chaos-<id>` name you own:

```bash
.bin/kind get clusters
.bin/kind delete cluster --name ahk-chaos-<id> \
  --kubeconfig .cache/cluster-evidence/<id>/kubeconfig
```

## Deployment limits

- One worker replica with SQLite on a ReadWriteOnce PVC
  ([ADR 0005](decisions/0005-single-worker-sqlite-pvc.md)). No horizontal scale-out.
- Temporal is the single-replica development server, without transport
  authentication. It is not a production Temporal cluster.
- Collector trace files, Prometheus and Grafana data are ephemeral.
- Default kindnet does not enforce NetworkPolicy. The policies state intent only.
- Grafana allows anonymous Viewer access to local synthetic data.
- No Langfuse, Tempo, ingress, external identity provider or live model endpoint.
