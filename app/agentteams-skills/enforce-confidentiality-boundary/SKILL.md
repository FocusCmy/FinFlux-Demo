---
name: enforce-confidentiality-boundary
description: Enforce a fail-closed raw-content and evidence-sharing boundary from an existing rights classification.
---

# Enforce Confidentiality Boundary

Consume only the output of `classify-data-rights`. Emit `PASS` or `NEEDS_EVIDENCE`, the permitted evidence scope, and whether model raw-content access is allowed. Hash-only review remains the default for non-public material. Never copy credentials, full confidential documents or unapproved content into Matrix.
