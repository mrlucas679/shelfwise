"""Product catalog CRUD routes: products, variants, identifiers, and identity resolution.

Second API router split out of `app.py`'s single-file route list, following the same
pattern `routes_twin.py` established. Depends only on the shared `product_catalog_store`
singleton (`state.py`) and the tenant/write-path dependencies (`deps.py`) - no cross-talk
with chat, decisions, connectors, or the cascade pipeline. The CSV/connector intake
routes that also touch the catalog store stay in `app.py`: they share `_process_inbound_record`
and other connector-pipeline helpers that are not yet split out, and moving only half of
that coupling here would just relocate the tangle instead of resolving it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from shelfwise_catalog import (
    ConflictingIdentifierError,
    Product,
    ProductIdentifier,
    ProductVariant,
)

from .deps import CURRENT_TENANT_DEP, INGEST_AUTH_DEP, WRITE_LIMIT_DEP, write_path_guard
from .state import product_catalog_store
from .tenant import TenantContext

router = APIRouter()


class ProductUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=200)


class ProductVariantUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1, max_length=200)
    pack_size: str | None = Field(default=None, max_length=100)
    unit_of_measure: str | None = Field(default=None, max_length=50)
    is_case_pack: bool = False


class ProductIdentifierUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1, max_length=200)
    kind: str
    value: str = Field(min_length=1, max_length=200)
    source_system: str | None = Field(default=None, max_length=100)


@router.post("/catalog/products", dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP])
def upsert_product(
    body: ProductUpsertBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    product = Product(
        tenant_id=ctx.tenant_id,
        product_id=body.product_id,
        name=body.name,
        category=body.category,
        brand=body.brand,
    )
    return {"product": product_catalog_store.upsert_product(product)}


@router.get("/catalog/products")
def list_products(ctx: TenantContext = CURRENT_TENANT_DEP) -> dict[str, object]:
    return {"products": product_catalog_store.list_products(tenant_id=ctx.tenant_id)}


@router.get("/catalog/products/{product_id}")
def get_product(
    product_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    product = product_catalog_store.get_product(tenant_id=ctx.tenant_id, product_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"product": product}


@router.post(
    "/catalog/products/{product_id}/variants",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_product_variant(
    product_id: str,
    body: ProductVariantUpsertBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    variant = ProductVariant(
        tenant_id=ctx.tenant_id,
        variant_id=body.variant_id,
        product_id=product_id,
        pack_size=body.pack_size,
        unit_of_measure=body.unit_of_measure,
        is_case_pack=body.is_case_pack,
    )
    return {"variant": product_catalog_store.upsert_variant(variant)}


@router.get("/catalog/products/{product_id}/variants")
def list_product_variants(
    product_id: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    variants = product_catalog_store.list_variants(tenant_id=ctx.tenant_id, product_id=product_id)
    return {"variants": variants}


@router.post(
    "/catalog/identifiers",
    dependencies=[Depends(write_path_guard), WRITE_LIMIT_DEP],
)
def upsert_product_identifier(
    body: ProductIdentifierUpsertBody,
    ctx: TenantContext = INGEST_AUTH_DEP,
) -> dict[str, object]:
    try:
        identifier = ProductIdentifier(
            tenant_id=ctx.tenant_id,
            variant_id=body.variant_id,
            kind=body.kind,
            value=body.value,
            source_system=body.source_system,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return {"identifier": product_catalog_store.upsert_identifier(identifier)}
    except ConflictingIdentifierError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/catalog/resolve")
def resolve_product_identifier(
    kind: str,
    value: str,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> dict[str, object]:
    """Resolve a single source-system code (GTIN/barcode/SKU/PLU/source id) to a variant.

    This is the identity-resolution seam every downstream reasoning path (expiry,
    reorder, demand) needs before it can safely trust "which physical item is this".
    """
    variant = product_catalog_store.resolve_identifier(
        tenant_id=ctx.tenant_id, kind=kind, value=value
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="No variant resolves to that identifier")
    return {"variant": variant}
