# Local Temporal recovery evidence

Status: **passed**.

This uses real localhost Temporal and worker processes, not Kubernetes.
Source tree SHA256: `70bfbf57378121f0c72b38193f1a2e24fa084c8d3926dd7f9616d98acab9408b`.
Model spend: $0.

Recovery: 3/3 workflows completed after SIGKILL at `pending_approval`.
Recovery histories replayed: 3.
Lost-acknowledgement Activity retries observed: 1.
Owned processes stopped: True.

| Case | Status | Approval | Effects | Replay |
|---|---|---|---|---|
| recover-1 | completed | approved | 1 | True |
| recover-2 | completed | approved | 1 | True |
| recover-3 | completed | approved | 1 | True |
| reject | completed | rejected | 0 | True |
| duplicate | completed | approved | 1 | True |
| expire | completed | expired | 0 | True |

Limits: the fixture backend is deterministic and free; local SQLite and the
unauthenticated development Temporal service are not a production multi-host service.
The successful approval channel verifies the local kernel peer UID; direct Temporal access is trusted.
A started generation with unknown outcome fails closed; external API exactly-once execution is not claimed.
