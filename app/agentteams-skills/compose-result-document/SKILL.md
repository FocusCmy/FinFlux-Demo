---
name: compose-result-document
description: Generate plain-language FinFlux PDF, Markdown, and JSON reports from sealed structured results without changing financial truth.
---

# Compose Result Document

Use `preview` only when the Human Gate is `AWAITING_HUMAN`; label it as pending
and never imply production authorization. Use `final` only after an APPROVED,
REJECTED or RETURNED Human decision exists. Generate all formats from the same
payload hash.

```text
python scripts/run.py --run-json <run.json> --submission-json <submission.json> --stage preview --output-root <dir>
```

Do not summarize the full Matrix transcript. Financial values must be copied
from deterministic Skill outputs exactly.
