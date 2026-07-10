from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Turn:
    role: str
    text: str


def compact(
    turns: list[Turn],
    *,
    pinned: dict[str, str],
    budget_chars: int,
) -> tuple[list[Turn], dict[str, int]]:
    """Keep pinned facts and newest turns; visibly fold older overflow turns."""
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")
    pin_block = "; ".join(f"{key}={value}" for key, value in sorted(pinned.items()))
    fixed_chars = len(pin_block)
    kept: list[Turn] = []
    used = fixed_chars
    reversed_turns = list(reversed(turns))
    for index, turn in enumerate(reversed_turns):
        if used + len(turn.text) > budget_chars:
            folded = len(reversed_turns) - index
            break
        kept.append(turn)
        used += len(turn.text)
    else:
        folded = 0
    kept.reverse()
    output: list[Turn] = []
    if pin_block:
        output.append(Turn("tool", f"[pinned facts] {pin_block}"))
    if folded:
        output.append(
            Turn("tool", f"[compacted] {folded} earlier turn(s) folded; pinned facts retained.")
        )
    output.extend(kept)
    return output, {"folded": folded, "kept": len(kept), "chars": used}
