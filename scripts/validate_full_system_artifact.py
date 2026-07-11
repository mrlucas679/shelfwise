from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shelfwise_eval.full_system import (
    FeatureReceipt,
    RouteReceipt,
    audit_full_system_integrity,
)


def validate_artifact(artifact_dir: Path) -> dict[str, Any]:
    manifest = _read_json(artifact_dir / "manifest.json")
    trail = _read_jsonl(artifact_dir / "decision_trail.jsonl")
    feature_rows = _read_json(artifact_dir / "feature_receipts.json")
    route_rows = _read_json(artifact_dir / "route_receipts.json")
    totals = manifest.get("totals") if isinstance(manifest.get("totals"), dict) else {}
    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    features = [FeatureReceipt(**row) for row in feature_rows]
    routes = [RouteReceipt(**row) for row in route_rows]
    failures = audit_full_system_integrity(
        decision_trail=trail,
        feature_receipts=features,
        route_receipts=routes,
        live_required=bool(config.get("live_required")),
        chat_calls=int(totals.get("chat_calls") or 0),
        chat_model_answers=int(totals.get("chat_model_answers") or 0),
        chat_offline_answers=int(totals.get("chat_offline_answers") or 0),
        chat_errors=int(totals.get("chat_errors") or 0),
    )
    return {
        "artifact_dir": str(artifact_dir),
        "original_exit_code": manifest.get("exit_code"),
        "validated_exit_code": 1 if failures else 0,
        "passed": not failures,
        "failures": failures,
        "chat": {
            "calls": int(totals.get("chat_calls") or 0),
            "model_answers": int(totals.get("chat_model_answers") or 0),
            "offline_answers": int(totals.get("chat_offline_answers") or 0),
            "errors": int(totals.get("chat_errors") or 0),
        },
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Revalidate a completed ShelfWise harness run.")
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_artifact(args.artifact_dir)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return int(result["validated_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
