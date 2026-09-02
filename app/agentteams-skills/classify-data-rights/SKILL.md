---
name: classify-data-rights
description: Classify declared data rights, confidentiality and permitted usage without inventing legal authority.
---

# Classify Data Rights

Input is a run-scoped Rights Gate handle, declared rights basis, confidentiality class and permitted usage scope. Output must contain the normalized classification, whether a rights basis is present, and whether raw content may cross the model boundary.

Fail closed with `NEEDS_EVIDENCE` when the declaration is absent or unsupported. This Skill is not legal advice and must never convert a submitter declaration into institutional authorization.
