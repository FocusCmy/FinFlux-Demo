---
name: select-token-budget-strategy
description: Select a fail-closed report-generation strategy that avoids unnecessary model context and records the token boundary.
---

# Select Token Budget Strategy

Prefer `DETERMINISTIC_TEMPLATE_ONLY` whenever Run, DataPass, Human Gate and hashes
are structured. Never resend Matrix history or raw evidence to a model. If a
required field is missing, return `MANUAL_EVIDENCE_REQUIRED` rather than asking
a model to guess it.

An optional model summary is allowed only after explicit operator opt-in, with
at most 6000 input characters and 350 output tokens. It may improve wording but
must not alter financial values, recommendations or Human decisions.

```text
python scripts/run.py --context-json <context.json> --output <strategy.json>
```
