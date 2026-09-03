# FinFlux: Financial Semantic Admission and Controlled Evolution Engine

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/FocusCmy/FinFlux-Demo/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/FocusCmy/FinFlux-Demo/actions/workflows/ci.yml?query=branch%3Amain)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

FinFlux verifies financial data before it enters valuation, settlement, risk, research, or backtesting systems. It seals the source evidence, validates the intended business meaning, coordinates specialist review through AgentTeams, produces a DataPass, and preserves the final Human decision. An HTTP success response is never treated as proof that the financial semantics are correct. Models cannot rewrite financial values or grant final admission.

![FinFlux live intake](docs/screenshots/01-live-intake.png)

## Why FinFlux

Financial data failures are often semantic rather than syntactic: a field is populated and technically valid, but represents the wrong business concept for the downstream task. FinFlux makes that decision inspectable and accountable by separating deterministic calculation, model-assisted professional review, immutable evidence, and human authorization.

The runtime follows one auditable chain:

1. A user uploads a file, pastes financial text, or provides a public URL, then describes the downstream purpose in natural language.
2. The backend seals the original bytes, source declaration, and SHA-256 digest into an immutable `EvidenceBundle`.
3. A deterministic Profile extracts verifiable facts. Unknown or insufficient evidence returns `WAIT` with explicit missing items instead of a fabricated conclusion.
4. `RunSupervisor` advances the same Run in the background: the Manager selects a route, the Case Lead dispatches only the required Workers, and each Worker discovers and executes versioned Skills at runtime.
5. Sealed Worker artifacts are composed into a `DataPassDraft`. Every `PASS`, `WAIT`, or `BLOCK` recommendation reaches the Human Gate for approval, return, or confirmed rejection.
6. The signed result is exported as Markdown, PDF, JSON, and an audit ZIP. One Trace links model I/O, tool I/O, Skill versions and hashes, the provider Token ledger, and the Human signature.

![AgentTeams collaboration](docs/screenshots/02-agentteams-collaboration.png)

## Interface tour

The animation below is an approximately eight-second tour assembled from five real UI pages. It helps reviewers understand the operating sequence, but it is not presented as proof of model execution. A real multi-Agent Run is evidenced by Matrix events, sealed Worker artifacts, Skill receipts, provider Token records, and the Human signature bound to the same Run.

![FinFlux UI walkthrough](docs/demo/finflux-ui-walkthrough.gif)

| Live intake | AgentTeams collaboration |
| --- | --- |
| [![Live intake](docs/screenshots/01-live-intake.png)](docs/screenshots/01-live-intake.png) | [![AgentTeams collaboration](docs/screenshots/02-agentteams-collaboration.png)](docs/screenshots/02-agentteams-collaboration.png) |
| DataPass and Human Gate | Evaluation and observability |
| [![DataPass and Human Gate](docs/screenshots/03-datapass-human.png)](docs/screenshots/03-datapass-human.png) | [![Evaluation and observability](docs/screenshots/04-evaluation.png)](docs/screenshots/04-evaluation.png) |

See the full [Trace and recovery view](docs/screenshots/05-trace-recovery.png). A real execution video should be attached to a GitHub Release and linked here rather than committing a large MP4 into Git history.

## Public repository boundary

This repository includes the application source, required tests, AgentTeams deployment definitions, Agent and Skill packages, protocols, Docker files, and a source-bound 150-record manifest. `app/data/real_50x3_v1/manifest.json` contains 50 futures, 50 equity, and 50 fund records with source URLs, capture timestamps, source-file SHA-256 values, record SHA-256 values, and rights status.

The following are intentionally excluded from Git: API keys, Human credentials, runtime state, prompt and Token ledgers, historical Runs, audit ZIP files, videos, third-party raw market data, and embedded AgentTeams source code. Manifest sources are marked `REVIEW_REQUIRED`; evaluators and developers should upload data they are authorized to process when reproducing a live Run.

## Requirements

- Windows 10/11 with PowerShell 5.1/7, or Linux/macOS with Bash;
- Python 3.10–3.13; the Docker image is pinned to Python 3.12;
- Docker Desktop or Docker Engine 24+ with `docker compose`;
- the complete multi-Agent demonstration requires access to the AgentTeams v1.2.2 image and an OpenAI-compatible model API;
- the web application uses port `8768`; AgentTeams defaults to `18080`, `18001`, `18088`, and `18888`.

## Quick start: UI and deterministic core

No model credentials are required. This mode validates upload, EvidenceBundle sealing, Profile precheck, the UI, and the public API.

```powershell
git clone https://github.com/FocusCmy/FinFlux-Demo.git
cd FinFlux-Demo
docker compose up --build -d
```

Open the 16:9 product introduction at <http://127.0.0.1:8768/intro.html>, then enter the live Case workbench. The main application remains available at <http://127.0.0.1:8768>. Check health with:

```powershell
Invoke-RestMethod http://127.0.0.1:8768/api/status
docker compose ps
```

Stop the service with:

```powershell
docker compose down
```

This mode does not impersonate AgentTeams. If the Runtime is absent, the model path is shown as not ready while deterministic precheck remains available.

If the build stops at `failed to fetch anonymous token`, the host cannot reach Docker Hub authentication. Configure the Docker Desktop proxy or a registry mirror, verify that `python:3.12-slim` can be pulled, and retry. A reachable compatible base image can also be selected explicitly:

```powershell
$env:FINFLUX_PYTHON_IMAGE = '<reachable-registry>/library/python:3.12-slim'
docker compose up --build -d
```

## Source startup and validation

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action Validate
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action Start
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/start-local-demo.sh
```

## Full AgentTeams v1.2.2 live chain

Credentials must stay outside the repository. Copy the template first:

```powershell
Copy-Item .\agentteams\.env.example C:\finflux-runtime.env
notepad C:\finflux-runtime.env
```

Configure at least the following values. Use a model identifier supported by the selected provider:

```dotenv
AGENTTEAMS_LLM_PROVIDER=openai-compat
AGENTTEAMS_DEFAULT_MODEL=<model-name>
AGENTTEAMS_OPENAI_BASE_URL=<provider-base-url>
AGENTTEAMS_LLM_API_KEY=<api-key>
AGENTTEAMS_ADMIN_PASSWORD=<strong-local-password>
```

Fetch and verify the official AgentTeams source into the ignored `.cache` directory, build the eight routable Worker packages, perform a clean cold start, deploy the Runtime, and start FinFlux:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action BootstrapAgents
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action BuildAgents
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action ColdStart
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action Deploy -RuntimeEnvFile C:\finflux-runtime.env
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action Start -RuntimeEnvFile C:\finflux-runtime.env
```

`ColdStart` proves that a clean checkout can boot the Web/API layer and read the 150-record manifest. `Deploy` checks that the AgentTeams resources are genuinely ready. On `Start`, `RuntimeSupervisor` keeps Run creation closed until it has proved all five admission conditions: conflict-free Docker ports, an exact `8/8` Team quorum, authenticated AI Proxy readback pinned to the controlled `8090` route, byte-identical repository/container Worker packages, and one real provider canary with gateway-ledger Token usage. A failed step never generates fake Worker output.

Inspect the machine-readable gate at `GET /api/v1/runtime-supervisor`. The Live Intake page shows the same five receipts. If it enters `OPERATIONAL_WAIT`, use **Repair runtime environment** or call `POST /api/v1/runtime-supervisor/repair`; repair is bounded to three attempts, rebuilds only the failed Worker role, and never creates a business Run. Runtime logs are written under `app/runtime/runtime_supervisor/` and are excluded from Git.

If the Human account is generated by a custom resource, retrieve it with:

```powershell
powershell -ExecutionPolicy Bypass -File .\agentteams\scripts\Show-AgentTeamsHumanCredential.ps1
```

Store the returned username and password in `C:\finflux-runtime.env`, restart FinFlux, and never copy that file into the repository.

## Live demonstration

1. Open **Live Intake** and upload CSV, JSON, PDF, or text, paste financial text, or enter a public URL.
2. State the business purpose precisely: the target system, effective date, and semantic question. Example: `Verify whether this futures dataset is suitable for daily settlement P&L and explain the semantic basis for the selected field.`
3. Select **Start real AgentTeams review**. The browser only observes; `RunSupervisor` continues synchronizing Matrix, Worker artifacts, and the model-gateway ledger in the background.
4. Open **AgentTeams** to inspect Manager routing, Case Lead dispatch, Workers, and Skill versions. Open **Trace** for model, tool, and Token evidence.
5. When the Run reaches `AWAITING_HUMAN`, open **Human Gate** and approve, request additional evidence, or confirm the block. An Agent recommendation cannot authorize itself.
6. After signing, export the final report and audit ZIP.

![DataPass and Human Gate](docs/screenshots/03-datapass-human.png)

## State model

- `PASS`: evidence and contract support admission as a Human-approval candidate; it does not mean an Agent granted approval.
- `WAIT`: purpose, source, rights, effective time, or evidence is insufficient; the UI lists the required additions.
- `BLOCK`: a deterministic conflict or quantified impact has been observed; the UI presents evidence and a revision path. Human return creates a Child Run for review.
- `AWAITING_HUMAN`: multi-Agent review is complete and a responsible Human must make the final decision.

## Validation and contribution

Run the complete zero-model submission gate before committing:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action Validate
git status --short
```

The gate builds Worker packages, validates AgentTeams configuration, smoke-tests Skills, runs the public core test suite, and parses the frontend JavaScript. It does not call a model or incur provider cost. A live uploaded case triggers the full model chain; displayed Token usage must come from the provider gateway ledger and is never estimated by the frontend.

## Repository layout

```text
app/                             Backend, frontend, RunSupervisor, protocols, and tests
app/agentteams-skills/           Executable, versioned, hash-bound Skills
app/data/real_50x3_v1/           150-record manifest and aggregate evaluation; no raw snapshots
app/data/research_data_layer_v1/ Research and macro metadata, source links, and hashes; no raw text
agentteams/                      Agent/Skill packages, custom resources, deployment/recovery scripts
scripts/                         Unified startup and validation entry points
docs/screenshots/                Curated interface screenshots
Dockerfile                       FinFlux application image
docker-compose.yml               Local Web/deterministic-core startup
LICENSE                          Apache License 2.0
THIRD_PARTY_NOTICES.md           AgentTeams and financial-data boundaries
```

## License

FinFlux source code is licensed under the [Apache License 2.0](LICENSE). Third-party software and financial data are not relicensed by this repository; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
