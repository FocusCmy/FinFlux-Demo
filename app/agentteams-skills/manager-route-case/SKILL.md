---
name: manager-route-case
version: 1.0.0
description: Route a normalized FinFlux CaseEnvelope to the minimum bounded Worker set without reading raw financial values.
---

# Manager Route Case

Input is a hash-bound routing-facts envelope: identities, evidence/rights state,
declared downstream purpose, change indicators and execution budget state.

The Skill deterministically emits a versioned RouteDecision containing reason
codes, the selected Worker set and required Skill versions. It must never read
raw evidence, choose a financial value, calculate financial impact, sign a
DataPass, or replace the Human Gate.

Failure to verify this instruction, its manifest, its entrypoint, or the input
facts hash is fail-closed and no dispatch receipt may be written.
