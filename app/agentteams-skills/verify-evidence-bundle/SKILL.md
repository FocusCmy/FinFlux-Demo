---
name: verify-evidence-bundle
description: Independently verify required evidence files and SHA256 values before a DataPass recommendation reaches Human review.
---

# Verify Evidence Bundle

Use this skill in an isolated validation task. Do not rely on another Agent's summary.

Run:

```bash
python scripts/run.py --asset equity
```

If any file is missing or any hash differs, return `BLOCK`, preserve the mismatch list, and request Human review. Never repair or overwrite evidence inside this skill.
