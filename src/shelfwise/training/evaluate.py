from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean
from typing import Any

from .collator import apply_chat_template, messages_for_prompt
from .compatibility import validate_adapter_compatibility
from .config import TrainingConfig, load_training_config
from .dataset import TrainingRow, load_training_rows
from .runtime import timestamped_run_dir, write_json

AnswerGenerator = Callable[[list[dict[str, str]]], str]


def _score(row: TrainingRow, generated: str) -> dict[str, Any]:
    lower = generated.lower()
    expected = row.expected_output
    risk = str(expected["risk_level"]).lower()
    findings = [str(item).lower() for item in expected.get("findings", [])]
    actions = [str(item).lower() for item in expected.get("recommended_actions", [])]
    missing = [str(item).lower() for item in expected.get("missing_information", [])]
    grounded_terms = [item.type for item in row.evidence] + [
        item.description.lower().split(" ")[0] for item in row.evidence if item.description
    ]
    missing_detected = not missing or "missing" in lower or any(
        any(part in lower for part in item.split()[:2]) for item in missing
    )
    return {
        "risk_classification": "pass" if risk in lower else "review",
        "missing_info_detection": "pass" if missing_detected else "review",
        "hallucination_check": "review"
        if "guarantee" in lower or "certain" in lower
        else "pass",
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


def detect_reference_echo(expected: dict[str, Any], generated: str) -> dict[str, Any]:
    """Detect reference-derived answers that could otherwise game lexical scoring."""

    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    normalized_generated = _normalize(generated)
    normalized_canonical = _normalize(canonical)
    exact_or_embedded = bool(normalized_canonical) and (
        normalized_generated == normalized_canonical
        or normalized_canonical in normalized_generated
    )
    similarity = SequenceMatcher(None, normalized_generated, normalized_canonical).ratio()
    fragments = [
        normalized
        for value in _reference_strings(expected)
        if len(normalized := _normalize(value)) >= 12
    ]
    copied = sum(1 for fragment in fragments if fragment in normalized_generated)
    copied_field_ratio = copied / len(fragments) if fragments else 0.0
    expected_tokens = set(normalized_canonical.split())
    generated_tokens = normalized_generated.split()
    novel_token_ratio = (
        sum(1 for token in generated_tokens if token not in expected_tokens)
        / len(generated_tokens)
        if generated_tokens
        else 0.0
    )
    detected = (
        exact_or_embedded
        or similarity >= 0.92
        or (copied_field_ratio >= 0.9 and novel_token_ratio <= 0.35)
    )
    return {
        "detected": detected,
        "exact_or_embedded": exact_or_embedded,
        "similarity": round(similarity, 4),
        "copied_field_ratio": round(copied_field_ratio, 4),
        "novel_token_ratio": round(novel_token_ratio, 4),
    }


def _reference_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_reference_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_reference_strings(item))
        return result
    return []


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _fixture_generation(_messages: list[dict[str, str]]) -> str:
    return "Fixture-only evaluation: no model inference was performed."


def _build_adapter_generator(
    config: TrainingConfig,
    adapter_path: str | Path,
) -> AnswerGenerator:
    adapter_dir = Path(adapter_path)
    validate_adapter_compatibility(adapter_dir, config)

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoProcessor, AutoTokenizer

    processor = AutoProcessor.from_pretrained(
        config.model_name_or_path,
        revision=config.model_revision,
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(adapter_dir, trust_remote_code=True)
    base_model = AutoModelForCausalLM.from_pretrained(
        config.model_name_or_path,
        revision=config.model_revision,
        torch_dtype=torch.bfloat16 if config.bf16 else None,
        device_map="auto",
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base_model, adapter_dir)
    model.eval()
    input_device = model.get_input_embeddings().weight.device

    def generate(messages: list[dict[str, str]]) -> str:
        prompt = apply_chat_template(processor, messages)
        encoded = tokenizer(prompt, return_tensors="pt")
        model_inputs = {key: value.to(input_device) for key, value in encoded.items()}
        prompt_tokens = int(model_inputs["input_ids"].shape[-1])
        with torch.inference_mode():
            output = model.generate(
                **model_inputs,
                do_sample=False,
                max_new_tokens=config.evaluation.max_new_tokens,
                pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
            )
        generated_tokens = output[0][prompt_tokens:]
        return str(tokenizer.decode(generated_tokens, skip_special_tokens=True)).strip()

    return generate


def _evaluation_summary(
    records: list[dict[str, Any]],
    *,
    config: TrainingConfig,
    inference_performed: bool,
    generation_source: str,
) -> dict[str, Any]:
    count = len(records)
    risk_accuracy = mean(
        record["rubric"]["risk_classification"] == "pass" for record in records
    )
    actionability = mean(record["rubric"]["actionability_score"] for record in records)
    grounding = mean(record["rubric"]["evidence_grounding_score"] for record in records)
    echo_rate = mean(record["reference_echo"]["detected"] for record in records)
    empty_generations = sum(not record["generated_answer"].strip() for record in records)
    failures: list[str] = []
    if not inference_performed:
        failures.append("no generated model inference was performed")
    if empty_generations:
        failures.append(f"{empty_generations} generated answers were empty")
    if echo_rate > config.evaluation.max_reference_echo_rate:
        failures.append(
            f"reference echo rate {echo_rate:.4f} exceeds "
            f"{config.evaluation.max_reference_echo_rate:.4f}"
        )
    if risk_accuracy < config.evaluation.min_risk_accuracy:
        failures.append(
            f"risk accuracy {risk_accuracy:.4f} is below "
            f"{config.evaluation.min_risk_accuracy:.4f}"
        )
    if actionability < config.evaluation.min_mean_actionability_score:
        failures.append(
            f"actionability {actionability:.4f} is below "
            f"{config.evaluation.min_mean_actionability_score:.4f}"
        )
    if grounding < config.evaluation.min_mean_grounding_score:
        failures.append(
            f"grounding {grounding:.4f} is below "
            f"{config.evaluation.min_mean_grounding_score:.4f}"
        )
    return {
        "row_count": count,
        "generation_source": generation_source,
        "inference_performed": inference_performed,
        "runtime_target": config.runtime.training_target,
        "risk_accuracy": round(risk_accuracy, 4),
        "mean_actionability_score": round(actionability, 4),
        "mean_grounding_score": round(grounding, 4),
        "reference_echo_rate": round(echo_rate, 4),
        "empty_generations": empty_generations,
        "gate": {"passed": not failures, "failure_reasons": failures},
    }


def run_evaluation(
    config_path: str | Path,
    *,
    dry_run: bool = True,
    eval_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    adapter_path: str | Path | None = None,
    generator: AnswerGenerator | None = None,
) -> Path:
    repo_root = Path.cwd()
    config = load_training_config(config_path)
    rows = load_training_rows(
        eval_path or config.data.eval_path,
        repo_root=repo_root,
        strict=config.data.strict_evidence,
    )
    base_output_dir = Path(output_dir) if output_dir is not None else repo_root / config.output_dir
    run_dir = timestamped_run_dir(base_output_dir, "eval", timestamp=True)
    jsonl_path = run_dir / "eval_results.jsonl"
    markdown_path = run_dir / "eval_results.md"

    if dry_run and generator is not None:
        raise ValueError("dry_run cannot be combined with an inference generator")
    if dry_run:
        effective_generator = _fixture_generation
        generation_source = "fixture_only"
        inference_performed = False
    else:
        if generator is not None:
            effective_generator = generator
            generation_source = "generated_inference"
        elif adapter_path is not None:
            effective_generator = _build_adapter_generator(config, adapter_path)
            generation_source = "training_adapter_generation"
        else:
            raise ValueError("generated evaluation requires adapter_path or generator")
        inference_performed = True

    records: list[dict[str, Any]] = []
    for row in rows:
        prompt_messages = messages_for_prompt(row)
        generated = effective_generator(prompt_messages)
        record = {
            "id": row.id,
            "case_type": row.case_type,
            "prompt_messages": prompt_messages,
            "generated_answer": generated,
            "expected_answer": row.expected_output,
            "rubric": _score(row, generated),
            "reference_echo": detect_reference_echo(row.expected_output, generated),
        }
        records.append(record)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    summary = _evaluation_summary(
        records,
        config=config,
        inference_performed=inference_performed,
        generation_source=generation_source,
    )
    write_json(run_dir / "eval_summary.json", summary)
    lines = [
        "# ShelfWise Gemma 4 Multimodal Eval",
        "",
        f"- Generated inference performed: `{inference_performed}`",
        f"- Evaluation gate passed: `{summary['gate']['passed']}`",
        f"- Reference echo rate: `{summary['reference_echo_rate']}`",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {record['id']}",
                "",
                f"- Case type: `{record['case_type']}`",
                f"- Generated: {record['generated_answer']}",
                f"- Rubric: `{json.dumps(record['rubric'], sort_keys=True)}`",
                f"- Reference echo: `{record['reference_echo']['detected']}`",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"eval results: {jsonl_path}")
    print(f"eval report: {markdown_path}")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ShelfWise multimodal adapter outputs")
    parser.add_argument("--config", required=True)
    parser.add_argument("--adapter-path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate fixture/report wiring without inference; this mode cannot pass the gate.",
    )
    args = parser.parse_args()
    run_evaluation(
        args.config,
        dry_run=args.dry_run,
        adapter_path=args.adapter_path,
    )


if __name__ == "__main__":
    main()
