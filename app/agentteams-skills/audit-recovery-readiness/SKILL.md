---
name: audit-recovery-readiness
description: Audit checkpoint and failure-receipt readiness without claiming unexecuted failover evidence.
---

# Audit Recovery Readiness

Consume the execution-budget receipt and output checkpoint requirement, retry policy and failure-receipt requirement. Configuration may be labelled `READY_FOR_CHECKPOINTED_RUN`; it must not be labelled proven high availability unless a run-scoped interruption and recovery receipt exists.
