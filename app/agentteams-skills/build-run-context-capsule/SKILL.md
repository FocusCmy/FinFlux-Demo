---
name: build-run-context-capsule
description: Build one immutable, content-addressed Run context capsule so AgentTeams messages carry a hash reference instead of repeatedly carrying financial evidence context.
---

# Build Run Context Capsule

Use after deterministic intake and Manager routing, before the first Matrix
dispatch. The Skill accepts only structured evidence handles, contract facts,
route policy and selected roles. It must not store raw evidence bytes,
credentials or chain-of-thought.

The output is one SHA-256-addressed capsule containing an allowlisted slice per
selected role. Record input/output hashes, Skill version, cache status and zero
provider-token usage. Do not describe estimated character savings as observed
provider-token savings.

```text
python scripts/run.py --input context-input.json --output capsule.json --root context-cache
```
