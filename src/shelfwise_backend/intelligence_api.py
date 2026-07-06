from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from shelfwise_data import (
    DecisionOutcome,
    DeliveryReceipt,
    StockBatch,
    SupplierCoverRequest,
    plan_supplier_cover,
    reconcile_delivery,
    split_stock_by_fefo,
    summarize_outcome,
)

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


class StockBatchPayload(BaseModel):
    sku: str = Field(min_length=1)
    lot: str = Field(min_length=1)
    units: int = Field(ge=0)
    expiry_date: date
    received_date: date
    location: str = Field(min_length=1)


class FefoSplitRequest(BaseModel):
    sku: str = Field(min_length=1)
    as_of: date
    priority_window_days: int = Field(default=1, ge=0)
    batches: list[StockBatchPayload] = Field(min_length=1)


class DeliveryReconciliationRequest(BaseModel):
    sku: str = Field(min_length=1)
    ordered_units: int = Field(ge=0)
    asn_units: int = Field(ge=0)
    received_units: int = Field(ge=0)
    accepted_units: int = Field(ge=0)
    rejected_units: int = Field(default=0, ge=0)
    short_dated_units: int = Field(default=0, ge=0)


class SupplierCoverPlanRequest(BaseModel):
    sku: str = Field(min_length=1)
    units_on_hand: int = Field(ge=0)
    forecast_daily_units: Decimal = Field(gt=0)
    supplier_lead_time_days: Decimal = Field(gt=0)
    transfer_available_units: int = Field(default=0, ge=0)


class OutcomeSummaryRequest(BaseModel):
    sku: str = Field(min_length=1)
    action: str = Field(min_length=1)
    predicted_sell_through_units: int = Field(ge=0)
    actual_sell_through_units: int = Field(ge=0)
    predicted_waste_units: int = Field(ge=0)
    actual_waste_units: int = Field(ge=0)


@router.post("/stock/fefo-split")
def split_stock_endpoint(payload: FefoSplitRequest) -> dict[str, object]:
    try:
        result = split_stock_by_fefo(
            sku=payload.sku,
            as_of=payload.as_of,
            priority_window_days=payload.priority_window_days,
            batches=tuple(
                StockBatch(
                    sku=batch.sku,
                    lot=batch.lot,
                    units=batch.units,
                    expiry_date=batch.expiry_date,
                    received_date=batch.received_date,
                    location=batch.location,
                )
                for batch in payload.batches
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"batch_split": result.to_dict()}


@router.post("/deliveries/reconcile")
def reconcile_delivery_endpoint(
    payload: DeliveryReconciliationRequest,
) -> dict[str, object]:
    try:
        result = reconcile_delivery(
            DeliveryReceipt(
                sku=payload.sku,
                ordered_units=payload.ordered_units,
                asn_units=payload.asn_units,
                received_units=payload.received_units,
                accepted_units=payload.accepted_units,
                rejected_units=payload.rejected_units,
                short_dated_units=payload.short_dated_units,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"delivery_reconciliation": result.to_dict()}


@router.post("/suppliers/cover-plan")
def plan_supplier_cover_endpoint(payload: SupplierCoverPlanRequest) -> dict[str, object]:
    try:
        result = plan_supplier_cover(
            SupplierCoverRequest(
                sku=payload.sku,
                units_on_hand=payload.units_on_hand,
                forecast_daily_units=payload.forecast_daily_units,
                supplier_lead_time_days=payload.supplier_lead_time_days,
                transfer_available_units=payload.transfer_available_units,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"supplier_cover": result.to_dict()}


@router.post("/outcomes/summarize")
def summarize_outcome_endpoint(payload: OutcomeSummaryRequest) -> dict[str, object]:
    try:
        result = summarize_outcome(
            DecisionOutcome(
                sku=payload.sku,
                action=payload.action,
                predicted_sell_through_units=payload.predicted_sell_through_units,
                actual_sell_through_units=payload.actual_sell_through_units,
                predicted_waste_units=payload.predicted_waste_units,
                actual_waste_units=payload.actual_waste_units,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"learning_summary": result.to_dict()}
