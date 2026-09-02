from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-json", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = json.loads(Path(args.context_json).read_text(encoding="utf-8-sig"))
    context = source.get("context") or source
    datapass = context.get("datapass") or {}
    required = [context.get("run_id"), context.get("case_id"), datapass.get("machine_recommendation")]
    strategy = "DETERMINISTIC_TEMPLATE_ONLY" if all(required) else "MANUAL_EVIDENCE_REQUIRED"
    result = {
        "protocol": "FINFLUX_TOKEN_BUDGET_STRATEGY_V1.0",
        "strategy": strategy,
        "model_called": False,
        "provider_tokens": 0,
        "model_fallback_policy": "EXPLICIT_OPT_IN_ONLY",
        "model_fallback_max_input_chars": 6000,
        "model_fallback_max_output_tokens": 350,
        "context_sha256": source.get("context_sha256"),
    }
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    result["strategy_sha256"] = hashlib.sha256(raw).hexdigest()
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
