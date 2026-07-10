from __future__ import annotations


def ean13_check_digit(body12: str) -> int:
    """Calculate the EAN-13 mod-10 check digit for a 12-digit body."""
    if len(body12) != 12 or not body12.isdigit():
        raise ValueError("EAN-13 body must be 12 digits")
    total = sum((1 if index % 2 == 0 else 3) * int(digit) for index, digit in enumerate(body12))
    return (10 - total % 10) % 10


def make_ean13(seq: int, *, prefix: str = "20") -> str:
    """Build a restricted-range synthetic EAN-13 code."""
    if not prefix.startswith("2") or not prefix.isdigit() or len(prefix) > 12:
        raise ValueError("synthetic EAN prefix must be a restricted range beginning with 2")
    body = (prefix + f"{seq:0{12 - len(prefix)}d}")[:12]
    return body + str(ean13_check_digit(body))


def is_valid_ean13(code: str) -> bool:
    """Return true when a code has a valid EAN-13 check digit."""
    return len(code) == 13 and code.isdigit() and int(code[-1]) == ean13_check_digit(code[:12])


def make_plu(seq: int, *, organic: bool = False) -> str:
    """Build an illustrative IFPS-style PLU, with 9-prefix for organic produce."""
    base = 4000 + (seq % 1000)
    return f"9{base}" if organic else f"{base}"
