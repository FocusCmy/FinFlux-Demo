---
name: evidence-integrity
version: 1.0.0
description: Verify immutable evidence handles, source hashes, evidence-root bindings, and gateway attestations. Never infer missing evidence.
---

# evidence-integrity

Verify immutable evidence handles, source hashes, evidence-root bindings, and gateway attestations. Never infer missing evidence.

The runtime must verify this instruction digest and the package entrypoint
digest before execution. A missing, mismatched, or tampered manifest is
fail-closed and must not produce a success receipt.
