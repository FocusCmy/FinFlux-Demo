---
name: resolve-semantic-contract
description: Resolve the versioned equity, futures, or option semantic contract before financial data is admitted downstream.
---

# Resolve Semantic Contract

Use this skill before impact calculation. You must select exactly one asset class: `equity`, `futures`, or `option`.

Run:

```bash
python scripts/run.py --asset futures
```

Treat the JSON output as the authoritative local contract. Do not replace required fields or block conditions with model knowledge. If the asset is unsupported or the contract cannot be loaded, stop the case with `NEEDS_EVIDENCE`.
