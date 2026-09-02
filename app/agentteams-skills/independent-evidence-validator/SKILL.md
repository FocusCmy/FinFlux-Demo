---
name: independent-evidence-validator
version: 1.0.0
description: Independently recompute evidence, contract and calculation consistency. It must not reuse another Worker's conclusion as truth.
---

# independent-evidence-validator

Independently recompute evidence, contract and calculation consistency. It must not reuse another Worker's conclusion as truth.

The runtime must verify this instruction digest and the package entrypoint
digest before execution. A missing, mismatched, or tampered manifest is
fail-closed and must not produce a success receipt.
