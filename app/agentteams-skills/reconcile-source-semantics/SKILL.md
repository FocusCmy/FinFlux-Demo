---
name: reconcile-source-semantics
description: Deterministically reconcile observed source, field, version, and event-row differences for equity, futures, or option evidence.
---

# Reconcile Source Semantics

Use this Skill after evidence hash verification and before selecting a financial-use contract. It reports observed differences only; it does not decide whether `close`, `settle`, adjusted price, or a contract version is globally correct.

Run exactly one asset-scoped command:

```text
python scripts/run.py --asset futures
```

Return the JSON unchanged. Never edit evidence, infer missing source values, or turn a field difference into an institution loss claim.
