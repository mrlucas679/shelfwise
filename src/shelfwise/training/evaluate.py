from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .collator import completion_for_row, messages_for_prompt
from .config import load_training_config
from .dataset import TrainingRow, load_training_rows
from .runtime import timestamped_run_dir


def _score(row: TrainingRow, generated: str) -> dict[str, Any]:
    lower = generated.lower()
    expected = row.expected_output
    risk = str(expected["risk_level"])
    findings = [str(item).lower() for item in expected.get("findings", [])]
    actions = [str(item).lower() for item in expected.get("recommended_actions", [])]
    grounded_terms = [item.type for item in row.evidence] + [
        item.description.lower().split(" ")[0] for item in row.evidence if item.description
    ]
    return {
        "risk_classification": "pass" if risk in lower else "review",
        "missing_info_detection": "pass"
        if "missing" in lower or expected.get("missing_information")
        else "review",
        "hallucination_check": "review" if "guarantee" in lower or "certain" in lower else "pass",
        "actionability_score": round(
            sum(1 for action in actions if any(part in lower for part in action.split()[:2]))
            / max(len(actions), 1),
            2,
        ),
        "evidence_grounding_score": round(
            sum(1 for term in grounded_terms if term and term in lower)
            / max(len(grounded_terms), 1),
            2,
        ),
        "finding_recall": round(
            sum(1 for finding in findings if any(part in lower for part in finding.split()[:2]))
            / max(len(findings), 1),
            2,
        ),
    }


def _dry_run_generation(row: TrainingRow) -> str:
    expected = row.expected_output
    evidence_types = ", ".join(sorted(row.evidence_types)) or "text"
    return (
        f"{expected['summary']} Risk level: {expected['risk_level']}. "
        f"Grounded evidence types: {evidence_types}. "
        f"Recommended actions: {', '.join(expected.get('recommended_actions', []))}. "
        f"Missing information: {', '.join(expected.get('missing_information', []))}."
    )


def run_evaluation(
    config_path: str | Path,
    *,
    dry_run: bool = True,
    eval_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    repo_root = Path.cwd()
    config = load_training_config(config_path)
    rows = load_training_rows(
        eval_path or config.data.eval_path,
        repo_root=repo_root,
        strict=config.data.strict_evidence,
    )
    base_output_dir = Path(output_dir) if output_dir is not None else repo_root / config.output_dir
    output_dir = timestamped_run_dir(base_output_dir, "eval", timestamp=True)
    jsonl_path = output_dir / "eval_results.jsonl"
    markdown_path = output_dir / "eval_results.md"
    records: list[dict[str, Any]] = []
    for row in rows:
        generated = _dry_run_generation(row) if dry_run else completion_for_row(row)
        record = {
            "id": row.id,
            "case_type": row.case_type,
            "prompt_messages": messages_for_prompt(row),
            "generated_answer": generated,
            "expected_answer": row.expected_output,
            "rubric": _score(row, generated),
        }
        records.append(record)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    lines = ["# ShelfWise Gemma 4 Multimodal Eval", ""]
    for record in records:
        lines.extend(
            [
                f"## {record['id']}",
                "",
                f"- Case type: `{record['case_type']}`",
                f"- Generated: {record['generated_answer']}",
                f"- Rubric: `{json.dumps(record['rubric'], sort_keys=True)}`",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"eval results: {jsonl_path}")
    print(f"eval report: {markdown_path}")
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ShelfWise multimodal adapter outputs")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use expected-output echo generation; useful before the adapter is available.",
    )
    args = parser.parse_args()
    run_evaluation(args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
