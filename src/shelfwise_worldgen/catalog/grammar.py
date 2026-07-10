from __future__ import annotations

PACKS: dict[str, list[tuple[str, str, float]]] = {
    "can_bottle": [
        ("330ml", "can", 0.33),
        ("440ml", "can", 0.44),
        ("500ml", "bottle", 0.5),
        ("1L", "bottle", 1.0),
        ("1.5L", "bottle", 1.45),
        ("2L", "bottle", 1.9),
        ("6x330ml", "multipack", 1.8),
        ("12x330ml", "multipack", 3.4),
    ],
    "milk": [("500ml", "carton", 0.5), ("1L", "carton", 1.0), ("2L", "carton", 1.9)],
    "kg_staple": [
        ("500g", "pack", 0.5),
        ("1kg", "pack", 1.0),
        ("2.5kg", "pack", 2.4),
        ("5kg", "pack", 4.7),
        ("10kg", "bag", 9.0),
    ],
    "g_small": [
        ("100g", "pack", 0.5),
        ("150g", "pack", 0.7),
        ("200g", "pack", 0.9),
        ("250g", "pack", 1.0),
        ("500g", "pack", 1.9),
    ],
    "kg_meat": [("300g", "pack", 0.6), ("500g", "pack", 1.0), ("1kg", "pack", 1.95)],
    "ml_liquid": [
        ("250ml", "bottle", 0.5),
        ("500ml", "bottle", 1.0),
        ("750ml", "bottle", 1.4),
        ("1L", "bottle", 1.8),
        ("2L", "bottle", 3.4),
    ],
    "unit": [("each", "each", 1.0), ("4s", "pack", 3.6), ("6s", "pack", 5.2)],
    "produce_wt": [("per kg", "kg", 1.0), ("500g punnet", "punnet", 0.55), ("1kg bag", "bag", 1.0)],
    "roll": [("1s", "roll", 1.0), ("4s", "pack", 3.7), ("9s", "pack", 8.0)],
}

_ABBR = {
    "ORIGINAL": "ORIG",
    "CHOCOLATE": "CHOC",
    "STRAWBERRY": "STRAWB",
    "FLAVOURED": "FLAV",
    "VANILLA": "VAN",
    "ASSORTED": "ASST",
    "REGULAR": "REG",
    "MEDIUM": "MED",
    "LARGE": "LRG",
    "WASHING": "WASH",
    "POWDER": "PWD",
    "LIQUID": "LIQ",
    "CONCENTRATE": "CONC",
    "PREMIUM": "PREM",
}
_RECEIPT_MAX = 22


def receipt_name(name: str, size_label: str) -> str:
    """Uppercase, abbreviate, append size, and fit a till-width receipt name."""
    words = [_ABBR.get(word.upper(), word.upper()) for word in name.split()]
    tail = size_label.upper().replace(" ", "")
    return f"{' '.join(words)} {tail}".strip()[:_RECEIPT_MAX].rstrip()
