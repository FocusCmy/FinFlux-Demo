# Third-party notices and data boundaries

## AgentTeams

FinFlux integrates with the official AgentScope AgentTeams runtime:

- Project: <https://github.com/agentscope-ai/AgentTeams>
- Version: `v1.2.2`
- Commit: `849182af8e017168a5a200a87b1062142caf462d`
- Upstream license: Apache License 2.0
- Upstream source: not vendored in this repository

Deployment scripts use the pinned v1.2.2 runtime images and FinFlux CR/configuration. FinFlux-specific adapters, contracts, Skills and Demo code are distinct submission work; interoperability does not imply upstream endorsement.

## PyMuPDF

FinFlux uses PyMuPDF to create the immutable PDF copy of a signed result:

- Project: <https://pymupdf.readthedocs.io/>
- Package: `PyMuPDF==1.28.0`
- License: GNU Affero General Public License v3.0 or a commercial Artifex license

PyMuPDF remains a separately distributed dependency and is not relicensed by FinFlux's Apache-2.0 license. Deployers are responsible for selecting and complying with the applicable PyMuPDF license. The MD and JSON result formats remain inspectable without granting Agent authority to sign a decision.

## Public financial-data evidence

The reproducibility evidence refers to public information collected through or compared across:

- FinShare documentation: <https://finvfamily.github.io/finshare/quickstart.html>
- AKShare documentation: <https://akshare-hh.readthedocs.io/en/latest/installation.html>
- China Financial Futures Exchange public market information
- EastMoney data accessed through AKShare where recorded by the evidence manifest

Each evidence item retains source metadata, observation time and SHA256 reference. Original responses and adapter-output snapshots are intentionally omitted. The repository does not grant redistribution rights for third-party data. Users must review each source's current terms, robots policy, rate limits and licensing requirements before collection or production use.

## Models and credentials

No model API credential is distributed. `.env.example` contains only blank values and non-secret deployment defaults. A user who starts a new AgentTeams Run is responsible for the chosen provider's terms, charges and data-handling policy.

## Competition and production boundary

This repository is a competition proof of concept. It demonstrates deterministic evidence checks, multi-agent responsibility separation, Matrix traces and a Human Gate; it is not a licensed market-data product, trading recommendation, regulated approval engine, or production SLA commitment.
