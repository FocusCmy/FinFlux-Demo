---
name: validate-admission-package
description: Independently validate evidence, reconciliation, semantic contract, deterministic impact, and the proposed admission recommendation.
---

# Validate Admission Package

Use this Skill only in the Independent Validator role. It reruns all deterministic gates and returns `PASS` only when the evidence, source reconciliation, contract, impact, and remediation residual checks agree.

```text
python scripts/run.py --asset futures --scenario post_remediation_review
```

The returned `PASS` is an independent validation status, not a Human release signature. Preserve any `DISAGREEMENT` or `NEEDS_EVIDENCE`; never repair inputs inside this Skill.
