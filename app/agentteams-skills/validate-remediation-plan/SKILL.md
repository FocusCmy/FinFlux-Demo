---
name: validate-remediation-plan
description: Deterministically verify that a FinFlux remediation applies the expected configuration, preserves a rollback reference, and covers affected downstream tasks.
---

# Validate Remediation Plan

Use only after a ChangeSet and ImpactGraph exist. Validate plan completeness without modifying evidence or approving production.

The output must keep `production_approved=false` and `human_gate_required=true`. Missing rollback references or actions for affected/unknown tasks return `NEEDS_REVISION`.

Run:

```text
python scripts/run.py --input remediation-input.json
```

The input JSON contains `baseline_submission`, `remediation_submission`, `impact_graph`, and `plan`.

