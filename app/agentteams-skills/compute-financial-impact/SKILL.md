---
name: compute-financial-impact
description: Recompute downstream financial impact from preserved evidence without delegating numerical truth to the language model.
---

# Compute Financial Impact

Use this skill only after resolving the semantic contract. Select one asset class.

Run:

```bash
python scripts/run.py --asset option --scenario blocked
python scripts/run.py --asset option --scenario admissible
```

`blocked` and `admissible` must read the same immutable evidence bundle. They differ only in the proposed downstream semantic mapping. Quote the returned values exactly and preserve `observed` versus `impact_is_counterfactual`. You must not describe a counterfactual option mapping as an observed institution loss or describe an admissible control as a production approval.
