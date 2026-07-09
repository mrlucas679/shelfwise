from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_CASE_TYPES = {
    "shipment_damage",
    "supplier_risk",
    "stock_discrepancy",
    "delivery_dispute",
    "compliance_evidence",
    "general",
}
VALID_EVIDENCE_TYPES = {
    "image",
    "audio",
    "video",
    "pdf",
    "screenshot",
    "text",
    "structured_json",
}
VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class EvidenceItem:
    type: str
    path: str
    mime_type: str
    description: str
    timestamp: str
    metadata: dict[str, Any]
    resolved_path: Path | None = None
    available: bool = False
    fallback: str | None = None


@dataclass(frozen=True)
class TrainingRow:
    id: str
    case_type: str
    messages: list[dict[str, str]]
    evidence: list[EvidenceItem]
    expected_output: dict[str, Any]

    @property
    def evidence_types(self) -> set[str]:
        return {item.type for item in self.evidence}


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _resolve_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _validate_message(value: Any, row_id: str, index: int) -> dict[str, str]:
    message = _require_dict(value, f"{row_id}.messages[{index}]")
    role = message.get("role")
    content = message.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"{row_id}.messages[{index}].role is invalid: {role!r}")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{row_id}.messages[{index}].content must be non-empty text")
    return {"role": role, "content": content}


def _evidence_fallback(evidence_type: str, metadata: dict[str, Any]) -> str | None:
    if evidence_type == "audio":
        return str(metadata.get("fallback") or "transcript")
    if evidence_type == "video":
        return str(metadata.get("fallback") or "sampled_frames")
    return None


def _validate_evidence(
    value: Any,
    *,
    row_id: str,
    index: int,
    repo_root: Path,
    strict: bool,
) -> EvidenceItem:
    evidence = _require_dict(value, f"{row_id}.evidence[{index}]")
    evidence_type = evidence.get("type")
    if evidence_type not in VALID_EVIDENCE_TYPES:
        raise ValueError(f"{row_id}.evidence[{index}].type is invalid: {evidence_type!r}")
    path_value = evidence.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError(f"{row_id}.evidence[{index}].path must be non-empty text")
    metadata = evidence.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{row_id}.evidence[{index}].metadata must be an object")
    resolved = _resolve_path(repo_root, path_value)
    available = resolved.exists()
    if strict and not available:
        raise ValueError(f"{row_id}.evidence[{index}] missing local file: {resolved}")
    for frame_path in metadata.get("frame_paths", []):
        resolved_frame = _resolve_path(repo_root, str(frame_path))
        if strict and not resolved_frame.exists():
            raise ValueError(f"{row_id}.evidence[{index}] missing sampled frame: {resolved_frame}")
    return EvidenceItem(
        type=str(evidence_type),
        path=path_value,
        mime_type=str(evidence.get("mime_type") or "application/octet-stream"),
        description=str(evidence.get("description") or ""),
        timestamp=str(evidence.get("timestamp") or ""),
        metadata=metadata,
        resolved_path=resolved,
        available=available,
        fallback=_evidence_fallback(str(evidence_type), metadata),
    )


def _validate_expected(value: Any, row_id: str) -> dict[str, Any]:
    expected = _require_dict(value, f"{row_id}.expected_output")
    risk_level = expected.get("risk_level")
    if risk_level not in VALID_RISK_LEVELS:
        raise ValueError(f"{row_id}.expected_output.risk_level is invalid: {risk_level!r}")
    for key in ("summary", "findings", "recommended_actions", "missing_information"):
        if key not in expected:
            raise ValueError(f"{row_id}.expected_output missing {key}")
    return expected


def parse_training_row(raw: dict[str, Any], *, repo_root: Path, strict: bool = True) -> TrainingRow:
    row_id = raw.get("id")
    if not isinstance(row_id, str) or not row_id.strip():
        raise ValueError("training row id must be non-empty text")
    case_type = raw.get("case_type")
    if case_type not in VALID_CASE_TYPES:
        raise ValueError(f"{row_id}.case_type is invalid: {case_type!r}")
    messages_value = raw.get("messages")
    if not isinstance(messages_value, list) or not messages_value:
        raise ValueError(f"{row_id}.messages must be a non-empty list")
    messages = [
        _validate_message(message, row_id, index)
        for index, message in enumerate(messages_value)
    ]
    evidence_value = raw.get("evidence") or []
    if not isinstance(evidence_value, list):
        raise ValueError(f"{row_id}.evidence must be a list")
    evidence = [
        _validate_evidence(
            item,
            row_id=row_id,
            index=index,
            repo_root=repo_root,
            strict=strict,
        )
        for index, item in enumerate(evidence_value)
    ]
    expected = _validate_expected(raw.get("expected_output"), row_id)
    return TrainingRow(
        id=row_id,
        case_type=str(case_type),
        messages=messages,
        evidence=evidence,
        expected_output=expected,
    )


def load_training_rows(
    path: str | Path,
    *,
    repo_root: str | Path | None = None,
    strict: bool = True,
) -> list[TrainingRow]:
    jsonl_path = Path(path)
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    if not jsonl_path.is_absolute():
        jsonl_path = root / jsonl_path
    if not jsonl_path.exists():
        raise FileNotFoundError(f"dataset file not found: {jsonl_path}")
    rows: list[TrainingRow] = []
    for line_no, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{jsonl_path}:{line_no}: invalid JSON: {exc}") from exc
        rows.append(parse_training_row(raw, repo_root=root, strict=strict))
    if not rows:
        raise ValueError(f"dataset file has no rows: {jsonl_path}")
    return rows


def summarize_rows(rows: list[TrainingRow]) -> dict[str, Any]:
    by_case = Counter(row.case_type for row in rows)
    by_modality: Counter[str] = Counter()
    unavailable: list[str] = []
    for row in rows:
        if not row.evidence:
            by_modality["text"] += 1
        for item in row.evidence:
            by_modality[item.type] += 1
            if not item.available:
                unavailable.append(f"{row.id}:{item.path}")
    return {
        "row_count": len(rows),
        "case_types": dict(sorted(by_case.items())),
        "modalities": dict(sorted(by_modality.items())),
        "unavailable_evidence": unavailable,
    }
