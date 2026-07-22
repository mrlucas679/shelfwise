from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"), (1, ""))
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_ORDINAL_IRREGULAR = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_MAX_WORDS_MAGNITUDE = 999_999_999_999  # ceiling _under_1000 can spell out without overflow
_UNITS = {
    r"\u00b0c": "degrees",
    "kg": "kilograms",
    "km": "kilometres",
    "ml": "millilitres",
    "hrs": "hours",
    "min": "minutes",
    "hr": "hour",
    "l": "litres",
    "g": "grams",
    "h": "hours",
}
_ABBREV = {
    "approx.": "approximately",
    "approx": "approximately",
    "vs.": "versus",
    "vs": "versus",
    "e.g.": "for example",
    "i.e.": "that is",
    "etc.": "and so on",
    "incl.": "including",
}


def int_to_words(number: int) -> str:
    """Convert an integer into simple English words for speech."""
    if number == 0:
        return "zero"
    if number < 0:
        return "minus " + int_to_words(-number)
    if number > _MAX_WORDS_MAGNITUDE:
        # Beyond what the scale table can safely spell out (billions max) - read the
        # digits rather than risk an out-of-range word lookup on a hostile/malformed value.
        return _spell_digits(str(number))
    remaining = number
    parts: list[str] = []
    for scale, name in _SCALES:
        if remaining >= scale:
            parts.append(_under_1000(remaining // scale))
            if name:
                parts.append(name)
            remaining %= scale
    return " ".join(parts)


def strip_markdown(text: str) -> str:
    """Remove visual markdown so spoken replies do not read formatting."""
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    stripped = re.sub(r"[*_`#>]+", "", stripped)
    stripped = re.sub(r"^\s*[-\u2022]\s+", "", stripped, flags=re.MULTILINE)
    return stripped


def normalize_for_speech(text: str) -> str:
    """Rewrite domain-heavy text into words that sound natural aloud."""
    spoken = strip_markdown(text)
    spoken = re.sub(r"(?:R|ZAR)\s?\d+(?:[ ,]\d{3})*(?:\.\d+)?[kKmM]?", _money, spoken)
    spoken = re.sub(
        r"\b(?:SKU|PLU|item)\s*#?\s*(\d[\d-]*)",
        lambda match: "item " + _spell_digits(match.group(1)),
        spoken,
        flags=re.IGNORECASE,
    )
    spoken = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", _iso_date, spoken)
    spoken = re.sub(
        r"(\d+)\s?%",
        lambda match: int_to_words(int(match.group(1))) + " percent",
        spoken,
    )
    for unit, replacement in sorted(_UNITS.items(), key=lambda item: -len(item[0])):
        spoken = re.sub(rf"(?<=\d)\s?{unit}\b", f" {replacement}", spoken, flags=re.IGNORECASE)
    for abbreviation, replacement in _ABBREV.items():
        spoken = re.sub(rf"(?<!\w){re.escape(abbreviation)}(?!\w)", replacement, spoken)
    return re.sub(r"\s{2,}", " ", spoken).strip()


def _under_1000(number: int) -> str:
    """Convert a number below one thousand into words."""
    words: list[str] = []
    remaining = number
    if remaining >= 100:
        words += [_ONES[remaining // 100], "hundred"]
        remaining %= 100
        if remaining:
            words.append("and")
    if remaining >= 20:
        words.append(_TENS[remaining // 10])
        if remaining % 10:
            words.append(_ONES[remaining % 10])
    elif remaining > 0:
        words.append(_ONES[remaining])
    return " ".join(words)


def _ordinal(day: int) -> str:
    """Convert a calendar day into a spoken ordinal."""
    parts = int_to_words(day).split()
    last = parts[-1]
    if last in _ORDINAL_IRREGULAR:
        parts[-1] = _ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        parts[-1] = last[:-1] + "ieth"
    else:
        parts[-1] = last + "th"
    return " ".join(parts)


def _money(match: re.Match[str]) -> str:
    """Convert South African rand notation into speakable words.

    Scales the whole value (not just the integer part) by the k/m multiplier before
    splitting into rand and cents - "R1.5k" is R1,500, not R1,000. The previous
    whole-and-fraction split only multiplied the whole part and silently discarded the
    fraction whenever a multiplier suffix was present, understating any spoken
    fractional-thousand or fractional-million figure by up to just under 1x the
    multiplier (e.g. "R2.25m" was spoken as "two million rand" instead of "two million
    two hundred and fifty thousand rand") - a real error in a voice interface that
    speaks real monetary figures for retail decisions.
    """
    body = (
        match.group(0)
        .replace("ZAR", "")
        .replace("R", "")
        .replace(",", "")
        .replace(" ", "")
        .strip()
    )
    multiplier = 1
    if body[-1:].lower() == "k":
        multiplier = 1_000
        body = body[:-1]
    elif body[-1:].lower() == "m":
        multiplier = 1_000_000
        body = body[:-1]
    total_cents = int(
        (Decimal(body) * multiplier * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    rand, cents = divmod(total_cents, 100)
    words = int_to_words(rand) + " rand"
    if cents:
        words += " and " + int_to_words(cents) + " cents"
    return words


def _iso_date(match: re.Match[str]) -> str:
    """Convert an ISO calendar date into spoken date words."""
    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3))
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        # Not a real calendar date (a malformed/hostile match) - leave the original text
        # untouched rather than guess or raise.
        return match.group(0)
    return f"the {_ordinal(day)} of {_MONTHS[month - 1]} {int_to_words(year)}"


def _spell_digits(value: str) -> str:
    """Spell identifiers digit by digit."""
    return " ".join("oh" if char == "0" else _ONES[int(char)] for char in value if char.isdigit())
