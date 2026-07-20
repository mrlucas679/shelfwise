"""Token-accounted context assembly receipts for a bounded vLLM request window.

Implements the plan's Section 41.3 blueprint: every chat request carries an auditable
receipt of how its context window was spent - which sections, how many estimated tokens,
what was omitted - and the request must be rejected or further compacted BEFORE network
I/O when the receipt does not validate. The estimator is a conservative
characters-per-token heuristic; a served-tokenizer estimator can replace `estimate_tokens`
without changing the receipt contract.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL_WINDOW = 8_192
_CHARS_PER_TOKEN = 4  # conservative for English + JSON payloads


def estimate_tokens(text: str) -> int:
    """Conservative fallback estimator: ~4 chars/token, never returns zero for content."""
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ContextAllocation:
    system: int = 700
    skill_catalogue: int = 500
    tool_schemas: int = 1_000
    pinned_and_summary: int = 700
    recent_turns: int = 900
    episodes: int = 700
    live_evidence: int = 1_000
    tool_results: int = 900
    output_reserve: int = 1_200
    safety_margin: int = 592

    @property
    def total(self) -> int:
        return (
            self.system
            + self.skill_catalogue
            + self.tool_schemas
            + self.pinned_and_summary
            + self.recent_turns
            + self.episodes
            + self.live_evidence
            + self.tool_results
            + self.output_reserve
            + self.safety_margin
        )

    def validate(self, *, model_window: int = DEFAULT_MODEL_WINDOW) -> None:
        if self.total != model_window:
            raise ValueError(f"context allocation {self.total} != model window {model_window}")


@dataclass(frozen=True, slots=True)
class ContextReceipt:
    model_window: int
    estimated_input_tokens: int
    reserved_output_tokens: int
    section_tokens: dict[str, int]
    selected_memory_ids: tuple[str, ...]
    selected_skill_ids: tuple[str, ...]
    selected_tools: tuple[str, ...]
    omitted_counts: dict[str, int]
    truncated: bool

    def validate(self) -> None:
        used = self.estimated_input_tokens + self.reserved_output_tokens
        if used > self.model_window:
            raise ValueError(f"context overflow: {used} > {self.model_window}")

    def to_dict(self) -> dict[str, object]:
        return {
            "model_window": self.model_window,
            "estimated_input_tokens": self.estimated_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "section_tokens": dict(self.section_tokens),
            "selected_memory_ids": list(self.selected_memory_ids),
            "selected_skill_ids": list(self.selected_skill_ids),
            "selected_tools": list(self.selected_tools),
            "omitted_counts": dict(self.omitted_counts),
            "truncated": self.truncated,
        }


def build_context_receipt(
    *,
    sections: dict[str, str],
    selected_memory_ids: tuple[str, ...] = (),
    selected_skill_ids: tuple[str, ...] = (),
    selected_tools: tuple[str, ...] = (),
    omitted_counts: dict[str, int] | None = None,
    truncated: bool = False,
    allocation: ContextAllocation | None = None,
    model_window: int = DEFAULT_MODEL_WINDOW,
) -> ContextReceipt:
    """Account every context section and fail closed on overflow before network I/O."""
    resolved_allocation = allocation or ContextAllocation()
    resolved_allocation.validate(model_window=model_window)
    section_tokens = {name: estimate_tokens(text) for name, text in sections.items()}
    receipt = ContextReceipt(
        model_window=model_window,
        estimated_input_tokens=sum(section_tokens.values()),
        reserved_output_tokens=resolved_allocation.output_reserve,
        section_tokens=section_tokens,
        selected_memory_ids=selected_memory_ids,
        selected_skill_ids=selected_skill_ids,
        selected_tools=selected_tools,
        omitted_counts=omitted_counts or {},
        truncated=truncated,
    )
    receipt.validate()
    return receipt
