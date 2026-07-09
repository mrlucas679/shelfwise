from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Gs1Parts:
    gtin: str | None = None
    lot: str | None = None
    expiry_yymmdd: str | None = None


class IdentityMap:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], str] = {}

    def link(
        self,
        product_id: str,
        *,
        sku: str | None = None,
        gtin: str | None = None,
        barcode: str | None = None,
        source_id: str | None = None,
    ) -> None:
        for kind, value in {
            "sku": sku,
            "gtin": gtin,
            "barcode": barcode,
            "source_id": source_id,
        }.items():
            if value:
                self._by_key[(kind, value)] = product_id

    def resolve(
        self,
        *,
        sku: str | None = None,
        gtin: str | None = None,
        barcode: str | None = None,
        source_id: str | None = None,
    ) -> str | None:
        for kind, value in {
            "gtin": gtin,
            "barcode": barcode,
            "sku": sku,
            "source_id": source_id,
        }.items():
            if value and (kind, value) in self._by_key:
                return self._by_key[(kind, value)]
        return None


def parse_gs1(value: str) -> Gs1Parts:
    text = value.strip()
    gtin = _after_ai(text, "01", 14)
    expiry = _after_ai(text, "17", 6)
    lot = None
    lot_index = text.find("(10)")
    if lot_index >= 0:
        lot = text[lot_index + 4 :].split("(")[0] or None
    return Gs1Parts(gtin=gtin, lot=lot, expiry_yymmdd=expiry)


def _after_ai(value: str, ai: str, length: int) -> str | None:
    marker = f"({ai})"
    index = value.find(marker)
    if index < 0:
        return None
    start = index + len(marker)
    parsed = value[start : start + length]
    return parsed if len(parsed) == length and parsed.isdigit() else None
