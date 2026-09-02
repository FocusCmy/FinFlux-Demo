---
name: assemble-run-result-context
description: Build a minimal hash-bound result context from one FinFlux Run without replaying the full Matrix transcript.
---

# Assemble Run Result Context

Use after a DataPassDraft exists. Read only the named Run and Submission JSON.
Keep IDs, precheck, DataPass, Human Gate, Worker seals, Skill versions, provider
usage and evidence hashes. Do not copy chat history, raw research text or model
reasoning into the reporting context.

```text
python scripts/run.py --run-json <run.json> --submission-json <submission.json> --output <context.json>
```

The output must include a canonical SHA256 and source/compact character counts.
Missing Run, evidence or DataPass fields fail closed.
