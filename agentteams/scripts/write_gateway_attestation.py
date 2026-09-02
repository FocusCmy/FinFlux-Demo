from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def canonical_sha256(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--provider-name", required=True)
    parser.add_argument("--provider-custom-url", required=True)
    parser.add_argument("--expected-gateway-url", required=True)
    args = parser.parse_args()
    payload = {
        "protocol": "FINFLUX_MODEL_GATEWAY_ROUTE_ATTESTATION_V1",
        "status": (
            "READY"
            if args.provider_custom_url.rstrip("/")
            == args.expected_gateway_url.rstrip("/")
            else "BLOCKED"
        ),
        "provider_name": args.provider_name,
        "provider_custom_url": args.provider_custom_url,
        "expected_gateway_url": args.expected_gateway_url,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload["attestation_sha256"] = canonical_sha256(payload)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    if payload["status"] != "READY":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
