---
name: load-role-context-slice
description: Resolve a SHA-256 Run context reference and expose only the allowlisted slice for the assigned AgentTeams role.
---

# Load Role Context Slice

Use inside the allowlisted Tool Gateway. Validate the capsule hash, Case/Run
binding, selected role and slice hash before returning structured fields.
Reject path traversal, unknown roles and tampered content. The Skill never
calls a model and never returns another role's fields.

```text
python scripts/run.py --capsule-ref <sha256> --role <role> --case-id <case> --run-id <run> --root context-cache --output role-slice.json
```
