from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from shelfwise_worldgen.catalog.sample import sample_assortment

from .config import DEFAULT_MIXTURE_WEIGHTS
from .dataset import load_training_rows, summarize_rows


@dataclass(frozen=True)
class SimProduct:
    sku: str
    name: str
    category: str
    supplier: str
    price_cents: int
    cost_cents: int
    refrigerated: bool
    shelf_life_days: int
    base_daily_units: int


def _generated_products() -> tuple[SimProduct, ...]:
    """Build training products from the same generated catalog used by the app."""
    return tuple(
        SimProduct(
            sku=product.sku,
            name=product.name,
            category=product.category,
            supplier=product.supplier,
            price_cents=product.price_cents,
            cost_cents=round(product.price_cents * 0.65),
            refrigerated=product.cat.refrigerated,
            shelf_life_days=product.cat.shelf_life_days,
            base_daily_units=product.cat.base_daily_units,
        )
        for product in sample_assortment(20_260_712, size=24)
    )


PRODUCTS = _generated_products()

CASE_TYPES = (
    "damaged goods during transport",
    "missing stock",
    "supplier delay",
    "fake delivery proof",
    "warehouse worker voice complaint",
    "customer dispute with screenshots",
    "proof-of-delivery mismatch",
    "product quality failure",
    "inventory reconciliation issue",
    "high-risk supplier pattern",
    "normal safe transaction",
    "ambiguous missing evidence",
)

MIXTURE_WEIGHTS = DEFAULT_MIXTURE_WEIGHTS

EVIDENCE_BY_CASE = {
    "damaged goods during transport": [
        {
            "type": "image",
            "path": "data/evidence/smoke/damaged_yogurt_photo.svg",
            "mime_type": "image/svg+xml",
            "description": "Receiving-bay damage photo from the simulated delivery.",
            "timestamp": "2026-07-09T06:35:00+02:00",
            "metadata": {"source": "world_simulation", "camera": "receiving_bay_1"},
        },
        {
            "type": "video",
            "path": "data/evidence/smoke/damaged_pallet_clip.txt",
            "mime_type": "text/plain",
            "description": "Frame-sampled video fallback for transport damage.",
            "timestamp": "2026-07-09T06:38:00+02:00",
            "metadata": {
                "fallback": "sampled_frames",
                "frame_paths": ["data/evidence/smoke/damaged_pallet_frame_001.svg"],
            },
        },
    ],
    "warehouse worker voice complaint": [
        {
            "type": "audio",
            "path": "data/evidence/smoke/warehouse_voice_transcript.txt",
            "mime_type": "text/plain",
            "description": "Transcript-first fallback from a warehouse worker voice complaint.",
            "timestamp": "2026-07-09T06:42:00+02:00",
            "metadata": {"fallback": "transcript", "speaker_role": "warehouse_worker"},
        }
    ],
    "customer dispute with screenshots": [
        {
            "type": "screenshot",
            "path": "data/evidence/smoke/delivery_dispute_screenshot.svg",
            "mime_type": "image/svg+xml",
            "description": "Customer-service screenshot showing a disputed proof of delivery.",
            "timestamp": "2026-07-09T06:45:00+02:00",
            "metadata": {"source": "supplier_portal"},
        }
    ],
    "proof-of-delivery mismatch": [
        {
            "type": "screenshot",
            "path": "data/evidence/smoke/delivery_dispute_screenshot.svg",
            "mime_type": "image/svg+xml",
            "description": "POD screenshot with signed quantity different from receiving count.",
            "timestamp": "2026-07-09T06:45:00+02:00",
            "metadata": {"source": "supplier_portal"},
        },
        {
            "type": "structured_json",
            "path": "data/evidence/smoke/product_metadata.json",
            "mime_type": "application/json",
            "description": "Receiving metadata for crate count and temperature.",
            "timestamp": "2026-07-09T06:30:00+02:00",
            "metadata": {"source": "receiving_system"},
        },
    ],
}


def build_shakedown_datasets(
    *,
    output_dir: Path,
    repo_root: Path,
    seed: int = 20260710,
    train_examples: int = 120,
    eval_examples: int = 12,
    mixture_weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Generate world-simulation training/eval JSONL plus a mixture report."""

    output_dir.mkdir(parents=True, exist_ok=True)
    train_path = output_dir / "shelfwise_world_shakedown_train.jsonl"
    eval_path = output_dir / "shelfwise_world_shakedown_eval.jsonl"
    effective_mixture = dict(mixture_weights or MIXTURE_WEIGHTS)
    rows = list(
        _generate_rows(
            seed=seed,
            count=train_examples + eval_examples,
            mixture_weights=effective_mixture,
        )
    )
    train_rows = rows[:train_examples]
    eval_rows = rows[train_examples:]
    _write_jsonl(train_path, train_rows)
    _write_jsonl(eval_path, eval_rows)
    strict_train = load_training_rows(train_path, repo_root=repo_root, strict=True)
    strict_eval = load_training_rows(eval_path, repo_root=repo_root, strict=True)
    report = {
        "train_path": str(train_path),
        "eval_path": str(eval_path),
        "mixture_weights": effective_mixture,
        "source_generators": ["shelfwise_worldgen", "shelfwise_synthdata"],
        "train_summary": summarize_rows(strict_train),
        "eval_summary": summarize_rows(strict_eval),
        "dataset_mixture_breakdown": dict(Counter(row["mixture"] for row in rows)),
        "case_breakdown": dict(Counter(row["case_label"] for row in rows)),
        "examples_dropped": 0,
        "examples_repaired": 0,
        "invalid_examples": 0,
    }
    (output_dir / "mixture_summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _generate_rows(
    *,
    seed: int,
    count: int,
    mixture_weights: Mapping[str, float],
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    base = date(2026, 7, 10)
    mixture_names = list(mixture_weights)
    weights = [mixture_weights[name] for name in mixture_names]
    canonical_events, golden_scenarios = _shared_generator_context(seed, count)
    for index in range(count):
        product = rng.choice(PRODUCTS)
        case_label = CASE_TYPES[index % len(CASE_TYPES)]
        mixture = rng.choices(mixture_names, weights=weights, k=1)[0]
        event = _world_event(seed, index, product, case_label, base)
        event["canonical_world_event"] = canonical_events[index % len(canonical_events)]
        event["synthetic_golden_scenario"] = golden_scenarios[index % len(golden_scenarios)]
        rows.append(_row_from_event(index, product, case_label, mixture, event))
    return rows


def _shared_generator_context(
    seed: int,
    count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read canonical world and golden-scenario APIs without owning those packages."""

    from shelfwise_synthdata.generators import CATEGORIES, generate_golden
    from shelfwise_worldgen.world import World, WorldConfig

    canonical_events = [event.to_dict() for event in World(WorldConfig(seed=seed)).run()]
    per_category = max(1, (count + len(CATEGORIES) - 1) // len(CATEGORIES))
    golden_scenarios = [
        {
            "id": scenario.id,
            "category": scenario.category,
            "expected": scenario.expected,
            "invariants": scenario.invariants,
            "synthetic_tag": scenario.tag.model_dump(mode="json"),
        }
        for scenario in generate_golden(seed, n_per_category=per_category)
    ]
    if not canonical_events or not golden_scenarios:
        raise RuntimeError("worldgen and synthdata must each produce at least one record")
    return canonical_events, golden_scenarios


def _world_event(
    seed: int,
    index: int,
    product: SimProduct,
    case_label: str,
    base: date,
) -> dict[str, Any]:
    current = base + timedelta(days=index % 7)
    rng = random.Random(f"{seed}:{product.sku}:{index}")
    opening = product.base_daily_units * (4 + index % 4)
    sold = max(1, round(product.base_daily_units * rng.uniform(0.85, 1.35)))
    received = 32 if case_label in {"proof-of-delivery mismatch", "missing stock"} else 36
    invoiced = 36
    temperature_c = 9.2 if product.refrigerated and case_label != "normal safe transaction" else 4.0
    return {
        "event_id": f"world_evt_{seed}_{index:04d}",
        "ts": datetime.combine(current, time(8 + index % 10), tzinfo=UTC).isoformat(),
        "store_id": "store_obs_main",
        "area": "observatory_blk7",
        "load_shedding_stage": 4 if current.day % 2 == 0 else 2,
        "sku": product.sku,
        "product": product.name,
        "category": product.category,
        "supplier": product.supplier,
        "opening_units": opening,
        "sold_units": sold,
        "on_hand_after_sales": opening - sold,
        "invoiced_crates": invoiced,
        "received_crates": received,
        "temperature_c": temperature_c,
        "temperature_limit_c": 5.0 if product.refrigerated else None,
        "case_label": case_label,
    }


def _row_from_event(
    index: int,
    product: SimProduct,
    case_label: str,
    mixture: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    risk_level = _risk_level(case_label, event)
    expected = _expected_output(case_label, risk_level, event)
    return {
        "id": f"world-mm-{index:04d}",
        "case_type": _case_type(case_label),
        "case_label": case_label,
        "mixture": mixture,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are ShelfWise. Use the simulated retail world, attached evidence, "
                    "and structured metadata to make grounded supply-chain decisions."
                ),
            },
            {
                "role": "user",
                "content": _user_prompt(case_label, mixture, product, event),
            },
            {
                "role": "assistant",
                "content": json.dumps(expected, sort_keys=True, separators=(",", ":")),
            },
        ],
        "evidence": _evidence(case_label, event),
        "expected_output": expected,
    }


def _case_type(case_label: str) -> str:
    if case_label in {"damaged goods during transport", "product quality failure"}:
        return "shipment_damage"
    if case_label in {"supplier delay", "high-risk supplier pattern"}:
        return "supplier_risk"
    if case_label in {"missing stock", "inventory reconciliation issue"}:
        return "stock_discrepancy"
    if case_label in {"fake delivery proof", "proof-of-delivery mismatch"}:
        return "delivery_dispute"
    if case_label == "warehouse worker voice complaint":
        return "compliance_evidence"
    return "general"


def _risk_level(case_label: str, event: dict[str, Any]) -> str:
    if case_label in {
        "damaged goods during transport",
        "warehouse worker voice complaint",
        "fake delivery proof",
        "high-risk supplier pattern",
        "product quality failure",
    }:
        return "high"
    if case_label == "normal safe transaction":
        return "low"
    if case_label == "ambiguous missing evidence":
        return "medium"
    if event["received_crates"] < event["invoiced_crates"]:
        return "medium"
    return "medium"


def _expected_output(case_label: str, risk_level: str, event: dict[str, Any]) -> dict[str, Any]:
    findings = [
        f"World simulation event {event['event_id']} for SKU {event['sku']}",
        f"Case label: {case_label}",
    ]
    actions = ["Record decision trace", "Cite every evidence source"]
    missing = []
    if event["received_crates"] < event["invoiced_crates"]:
        findings.append(
            "Received "
            f"{event['received_crates']} crates against {event['invoiced_crates']} invoiced"
        )
        actions.append("Open supplier delivery dispute")
    if event["temperature_limit_c"] and event["temperature_c"] > event["temperature_limit_c"]:
        findings.append("Cold-chain temperature exceeds allowed limit")
        actions.append("Quarantine affected refrigerated stock")
    if "ambiguous" in case_label:
        missing.extend(["Signed POD", "Temperature probe log", "Manager inspection photo"])
        actions.append("Ask for missing evidence before recommending write-back")
    if case_label == "normal safe transaction":
        actions = ["Continue monitoring", "Do not exaggerate risk"]
    summary = (
        f"{case_label.title()} requires {risk_level} risk handling for {event['product']}."
    )
    return {
        "summary": summary,
        "risk_level": risk_level,
        "findings": findings,
        "recommended_actions": actions,
        "missing_information": missing,
    }


def _user_prompt(
    case_label: str,
    mixture: str,
    product: SimProduct,
    event: dict[str, Any],
) -> str:
    if mixture == "tool_call_structured":
        instruction = "Return a tool-call-like structured decision report if action is needed."
    elif mixture == "report_action":
        instruction = "Write a manager-ready incident report with actions and missing evidence."
    elif mixture == "multimodal_evidence":
        instruction = "Interpret all visual/audio/video fallback evidence honestly."
    elif mixture == "simulation_incident":
        instruction = "Use the world simulation metadata as the primary source of truth."
    else:
        instruction = "Reason through the supply-chain decision and cite the strongest evidence."
    return (
        f"{instruction}\n"
        f"Case: {case_label}.\n"
        f"Product: {product.name} ({product.sku}) from {product.supplier}.\n"
        f"World event metadata: {json.dumps(event, sort_keys=True)}"
    )


def _evidence(case_label: str, event: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = list(EVIDENCE_BY_CASE.get(case_label, []))
    evidence.append(
        {
            "type": "structured_json",
            "path": "data/evidence/smoke/product_metadata.json",
            "mime_type": "application/json",
            "description": "Structured receiving metadata linked to the simulation event.",
            "timestamp": event["ts"],
            "metadata": {"world_event": event},
        }
    )
    if case_label == "normal safe transaction":
        evidence = [
            {
                "type": "structured_json",
                "path": "data/evidence/smoke/product_metadata.json",
                "mime_type": "application/json",
                "description": "Safe transaction metadata with no visible incident evidence.",
                "timestamp": event["ts"],
                "metadata": {"world_event": event},
            }
        ]
    return evidence


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
