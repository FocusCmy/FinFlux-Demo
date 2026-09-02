---
name: verify-result-artifact
description: Verify FinFlux report files, manifest hashes, Run binding, and pending-versus-final Human authorization boundaries.
---

# Verify Result Artifact

Read the generated Manifest and recompute every listed file SHA256. Require the
Manifest Run ID to match the requested Run. A preview must state that Human
authorization is pending; a final report must contain a recorded Human
disposition. Return failure without repairing or replacing any file.

```text
python scripts/run.py --manifest <manifest.json> --artifact-root <dir>
```
