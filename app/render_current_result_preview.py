from __future__ import annotations

import hashlib
import json
from pathlib import Path

from decision_reports import _render_pdf, build_result_payload, render_markdown
from live_intake import LiveIntakeRepository


def main() -> None:
    demo_root = Path(__file__).resolve().parent
    repository = LiveIntakeRepository(demo_root / "runtime")
    index = json.loads(repository.index_path.read_text(encoding="utf-8-sig"))
    run_id = str(index.get("latest_run_id") or "")
    if not run_id:
        raise SystemExit("没有可用于预览的Live Run")

    run = repository.get_run(run_id)
    submission = repository.get_submission(str(run["submission_id"]))
    payload = build_result_payload(run, submission)
    preview_root = demo_root / "output" / "result_previews" / run_id
    preview_root.mkdir(parents=True, exist_ok=True)
    digest = str(payload["result_payload_sha256"])
    stem = f"FinFlux_Result_Preview_{run_id}_{digest[:12]}"
    md_path = preview_root / f"{stem}.md"
    pdf_path = preview_root / f"{stem}.pdf"
    json_path = preview_root / f"{stem}.json"

    md_path.write_text(render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _render_pdf(payload, pdf_path)
    manifest = {
        "protocol": "FINFLUX_RESULT_PREVIEW_MANIFEST_V1.0",
        "truth_boundary": "真实Run的只读中间结果预览；未伪造Human签署，不是生产授权。",
        "run_id": run_id,
        "human_gate_state": (run.get("human_gate") or {}).get("state"),
        "result_payload_sha256": digest,
        "files": {},
    }
    for kind, path in (("markdown", md_path), ("pdf", pdf_path), ("json", json_path)):
        manifest["files"][kind] = {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest_path = preview_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"manifest": str(manifest_path), **manifest["files"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
