# Local kind pod-kill recovery evidence

Status: **passed**.

Source revision `7bcb8c8b23f3faed182e5e96f93b64dbd7ff10ca`; source tree SHA256 `70bfbf57378121f0c72b38193f1a2e24fa084c8d3926dd7f9616d98acab9408b`; uncommitted source changes: False.
Owned cluster `ahk-chaos-b56b5753ca` removed and absent from `kind get clusters`: True.
Kubernetes v1.34.11; kind v0.33.0 go1.26.7 linux/amd64; helm v3.22.1+gd6c1a0e; Docker 28.0.4.
Environment: GitHub Actions runner (Linux x86_64); Docker resources: 2 CPUs, 7.75 GiB memory.
Total duration 210 s, including image build, cluster create and teardown.
Actual model spend: $0.

## Recovery

Kill point: `pending_approval` with Activities `prepare`, `reserve`, `generate` and `validate_settle` completed. Only the worker pod was force-deleted.
Worker pod deleted to replacement Ready: 5.4 s.
Runs surviving the killed worker pod: **3/3**; histories replayed after restart: 6/6.
Synthetic ticket effects: before kill 0, after recovery 3, final 4 (one per approved action); lost-acknowledgement Activity retries reusing the effect: 1.

| Case | Status | Approval | Effects | Replayed |
|---|---|---|---|---|
| recover-1 | completed | approved | 1 | True |
| recover-2 | completed | approved | 1 | True |
| recover-3 | completed | approved | 1 | True |
| reject | completed | rejected | 0 | True |
| duplicate | completed | approved | 1 | True |
| expire | completed | expired | 0 | True |

Failed, timed-out or incomplete runs: none.

## Deployment and telemetry checks

- Rendered-manifest policy check: True; strict server-side schema dry run: True.
- All 4 pods have CPU/memory requests and limits: True.
- PVCs bound: {'ahk-temporal': 'Bound', 'ahk-worker': 'Bound'}; worker `/data` mounted writable: True.
- Service types: ['ClusterIP']; Ingress objects: 0.
- Spans received by the restarted worker's Collector: True; canaries absent from traces: True and metrics: True.
- Prometheus observed requests: 3.0; Grafana dashboard loaded: True.

## Limits

Observed recovery of one single-node kind cluster on GitHub Actions runner (Linux x86_64), not a universal SLO or a
production availability guarantee. One worker replica with SQLite on a ReadWriteOnce PVC; the Temporal
service is the unauthenticated development server. The backend is the deterministic fixture backend.
Raw histories, logs and rendered manifests stay in the ignored `.cache/cluster-evidence/` directory.
