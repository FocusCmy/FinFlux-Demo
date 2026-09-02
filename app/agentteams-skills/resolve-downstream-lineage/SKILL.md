---
name: resolve-downstream-lineage
description: Resolve a FinFlux ChangeSet to explicitly declared downstream task dependencies and preserve missing lineage as UNKNOWN_IMPACT.
---

# Resolve Downstream Lineage

Use after `detect-version-change`. Match changed paths only against versioned task manifests supplied by the institution.

Never infer undocumented dependencies or claim no impact when a task has no lineage. Return `UNKNOWN_IMPACT` so Manager can request evidence or escalate.

Run:

```text
python scripts/run.py --input lineage-input.json
```

The input JSON contains `change_set` and `downstream_tasks`.

