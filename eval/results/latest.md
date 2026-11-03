# T1 offline fixture evaluation

Deterministic fixture backend; actual model spend: $0.
This is a local controls ablation, not a measured LLM quality improvement.

| Group | Baseline checks passed | Hardened checks passed |
|---|---|---|
| admission | 0/4 | 4/4 |
| injection | 0/3 | 3/3 |
| pii | 0/4 | 4/4 |
| refusal | 2/4 | 4/4 |
| retrieval | 0/12 | 12/12 |
| synthetic_provider_fault | 0/7 | 7/7 |
| tenant_isolation | 0/2 | 2/2 |
| tool_contract | 3/3 | 3/3 |

| Case | Group | Baseline | Hardened | Hardened reason |
|---|---|---|---|---|
| qa-acme-01 | retrieval | fail | pass | ok |
| qa-acme-02 | retrieval | fail | pass | ok |
| qa-acme-03 | retrieval | fail | pass | ok |
| qa-acme-04 | retrieval | fail | pass | ok |
| qa-acme-05 | retrieval | fail | pass | ok |
| qa-acme-06 | retrieval | fail | pass | ok |
| qa-beta-01 | retrieval | fail | pass | ok |
| qa-beta-02 | retrieval | fail | pass | ok |
| qa-beta-03 | retrieval | fail | pass | ok |
| qa-beta-04 | retrieval | fail | pass | ok |
| qa-beta-05 | retrieval | fail | pass | ok |
| qa-beta-06 | retrieval | fail | pass | ok |
| unknown-01 | refusal | pass | pass | insufficient_evidence |
| unknown-02 | refusal | pass | pass | insufficient_evidence |
| unknown-03 | refusal | fail | pass | insufficient_evidence |
| unknown-04 | refusal | fail | pass | insufficient_evidence |
| isolation-acme | tenant_isolation | fail | pass | insufficient_evidence |
| isolation-beta | tenant_isolation | fail | pass | insufficient_evidence |
| pii-output-acme | pii | fail | pass | ok |
| pii-input-acme | pii | fail | pass | ok |
| pii-output-beta | pii | fail | pass | ok |
| pii-input-beta | pii | fail | pass | ok |
| fault-unsupported-text | synthetic_provider_fault | fail | pass | unsupported_evidence |
| fault-unknown-citation | synthetic_provider_fault | fail | pass | unsupported_evidence |
| fault-cross-citation | synthetic_provider_fault | fail | pass | unsupported_evidence |
| fault-extra-field | synthetic_provider_fault | fail | pass | invalid_output |
| fault-missing-usage | synthetic_provider_fault | fail | pass | invalid_output |
| fault-timeout | synthetic_provider_fault | fail | pass | backend_failure |
| fault-usage-overrun | synthetic_provider_fault | fail | pass | usage_overrun |
| admission-token-budget | admission | fail | pass | budget_exhausted |
| admission-rate-limit | admission | fail | pass | rate_limited |
| admission-same-request-replay | admission | fail | pass | ok |
| admission-request-conflict | admission | fail | pass | request_conflict |
| injection-input-acme | injection | fail | pass | input_injection |
| injection-input-beta | injection | fail | pass | input_injection |
| injection-document | injection | fail | pass | insufficient_evidence |
| tool-unknown-tool | tool_contract | pass | pass | unsupported_tool_call |
| tool-tenant-override | tool_contract | pass | pass | unsupported_tool_call |
| tool-extra-path | tool_contract | pass | pass | unsupported_tool_call |

baseline: retrieval recall@4=1.0; extractive citation support=1.0; tenant leakage cases=31.
Refusals correct: 2/4; zero-backend-call checks: 5/9.

hardened: retrieval recall@4=1.0; extractive citation support=1.0; tenant leakage cases=0.
Refusals correct: 4/4; zero-backend-call checks: 9/9.

Limits:
- No live LLM or upstream application was benchmarked.
- Provider-fault cases are synthetic and reported separately.
- Extractive support is text provenance, not semantic answer correctness.
- Tenant identity is supplied by a trusted local caller; there is no HTTP authentication.
