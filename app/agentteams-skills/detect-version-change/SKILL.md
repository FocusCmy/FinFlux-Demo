---
name: detect-version-change
description: Deterministically compare two immutable FinFlux EvidenceBundles and emit an observed-only ChangeSet before financial interpretation.
---

# Detect Version Change

Use after both EvidenceBundles pass hash and rights checks. Compare only bounded evidence facts and return the JSON ChangeSet unchanged.

Do not decide whether a change is financially correct, infer missing values, or mutate either bundle. A profile mismatch or mutated raw evidence is a hard failure.

Run:

```text
python scripts/run.py --input change-input.json
```

The input JSON contains `baseline_submission` and `candidate_submission`.

