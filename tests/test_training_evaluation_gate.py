from __future__ import annotations

import json
from pathlib import Path

from shelfwise.training.config import load_training_config
from shelfwise.training.dataset import load_training_rows
from shelfwise.training.evaluate import detect_reference_echo, run_evaluation

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "train_gemma4_multimodal.yaml"


def test_dry_run_is_fixture_only_and_cannot_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)

    run_dir = run_evaluation(CONFIG, dry_run=True, output_dir=tmp_path)
    summary = json.loads((run_dir / "eval_summary.json").read_text(encoding="utf-8"))

    assert summary["generation_source"] == "fixture_only"
    assert summary["inference_performed"] is False
    assert summary["gate"]["passed"] is False
    assert "no generated model inference" in summary["gate"]["failure_reasons"][0]


def test_reference_echo_fails_even_when_lexical_scores_are_high(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(ROOT)
    config = load_training_config(CONFIG)
    rows = load_training_rows(config.data.eval_path, repo_root=ROOT, strict=True)
    answers = iter(
        json.dumps(row.expected_output, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    row_index = 0

    def echo_reference(messages: list[dict[str, str]]) -> str:
        nonlocal row_index
        assert all(message["role"] != "assistant" for message in messages)
        prompt = "\n".join(message["content"] for message in messages)
        assert str(rows[row_index].expected_output["summary"]) not in prompt
        row_index += 1
        return next(answers)

    run_dir = run_evaluation(
        CONFIG,
        dry_run=False,
        output_dir=tmp_path,
        generator=echo_reference,
    )
    summary = json.loads((run_dir / "eval_summary.json").read_text(encoding="utf-8"))

    assert summary["inference_performed"] is True
    assert summary["reference_echo_rate"] == 1.0
    assert summary["gate"]["passed"] is False
    assert any("reference echo rate" in reason for reason in summary["gate"]["failure_reasons"])


def test_echo_detector_allows_independent_reasoning() -> None:
    expected = {
        "summary": "Delivery mismatch requires medium risk handling.",
        "risk_level": "medium",
        "findings": ["Received 31 crates against 40 invoiced"],
        "recommended_actions": ["Open supplier delivery dispute"],
        "missing_information": [],
    }
    generated = (
        "Risk is medium. Nine crates are unaccounted for, so hold reconciliation and ask the "
        "supplier for signed receiving evidence."
    )

    assert detect_reference_echo(expected, generated)["detected"] is False
