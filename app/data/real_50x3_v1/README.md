# 50/50/50 public evidence boundary

This directory contains only the signed manifest and aggregate deterministic
evaluation artifacts for 50 futures, 50 equity and 50 fund source rows.

The original CFFEX and EastMoney/AKShare snapshots are intentionally not
redistributed. `manifest.json` preserves the source URL, capture timestamp,
adapter identity, source-artifact SHA256, row identity and record SHA256. Every
source is marked `REVIEW_REQUIRED`; public accessibility is not treated as a
redistribution licence.

To reproduce a live run, supply data that you are authorised to process through
the FinFlux upload, text or public-URL intake. FinFlux freezes that input into a
new EvidenceBundle and never substitutes the omitted files.

