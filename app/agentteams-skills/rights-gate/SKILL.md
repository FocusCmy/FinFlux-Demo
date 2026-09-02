---
name: rights-gate
version: 1.0.0
description: Evaluate only declared rights metadata and return PASS or NEEDS_EVIDENCE. Never invent legal authority.
---

# rights-gate

Evaluate only declared rights metadata and return PASS or NEEDS_EVIDENCE. Never invent legal authority.

The runtime must verify this instruction digest and the package entrypoint
digest before execution. A missing, mismatched, or tampered manifest is
fail-closed and must not produce a success receipt.
