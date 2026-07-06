from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shelfwise_action import DecisionStore
from shelfwise_data import load_seeded_scenario
from shelfwise_inference import OpenAICompatibleInferenceClient, load_inference_config
from shelfwise_memory import LearningStore

from .cascade import run_golden_cascade
from .intelligence_api import router as intelligence_router

app = FastAPI(title="ShelfWise", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(intelligence_router)
decision_store = DecisionStore()
learning_store = LearningStore()


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "shelfwise",
        "version": "0.1.0",
        "inference": load_inference_config().to_public_dict(),
    }


@app.get("/readiness")
def readiness() -> dict[str, object]:
    inference = load_inference_config().to_public_dict()
    gateway_status = (
        "offline-safe" if inference["provider"] == "offline" else "configured"
    )
    seed_status = "ok"
    try:
        load_seeded_scenario()
    except (FileNotFoundError, ValueError):
        seed_status = "error"

    return {
        "ready": True,
        "checks": {
            "backend": "ok",
            "golden_cascade": "ok",
            "hitl": "ok",
            "learning": "ok",
            "store_intelligence": "ok",
            "seed_data": seed_status,
            "inference_gateway": gateway_status,
        },
        "next_external_checks": [
            "Fireworks credential smoke",
            "AMD Developer Cloud MI300X/vLLM credential smoke",
            "Docker build after Docker Desktop engine starts",
            "Browser verification after frontend build",
        ],
    }


@app.get("/inference/config")
def inference_config() -> dict[str, object]:
    return load_inference_config().to_public_dict()


@app.get("/inference/smoke")
def inference_smoke() -> dict[str, object]:
    result = OpenAICompatibleInferenceClient().complete(
        agent="critic",
        system="You are the ShelfWise critic. Reply briefly.",
        user="Say ready if the inference gateway is reachable.",
        max_tokens=40,
    )
    return {"result": result.to_dict()}


@app.get("/data/seed/summary")
def seed_summary() -> dict[str, object]:
    return {"seed_data": load_seeded_scenario().to_dict()}


@app.post("/demo/golden")
def demo_golden() -> dict[str, object]:
    result = run_golden_cascade()
    result["decision"] = decision_store.upsert(result["decision"])
    return result


@app.get("/demo/golden")
def demo_golden_get() -> dict[str, object]:
    result = run_golden_cascade()
    result["decision"] = decision_store.upsert(result["decision"])
    return result


@app.get("/decisions")
def list_decisions() -> dict[str, object]:
    return {"decisions": decision_store.list()}


@app.get("/learning")
def learning_summary() -> dict[str, object]:
    return {
        "thresholds": learning_store.thresholds(),
        "events": learning_store.list_events(),
    }


@app.get("/decisions/{decision_id}")
def get_decision(decision_id: str) -> dict[str, object]:
    decision = decision_store.get(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"decision": decision}


@app.post("/decisions/{decision_id}/approve")
def approve_decision(decision_id: str) -> dict[str, object]:
    decision = decision_store.approve(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    learning_event = learning_store.record_approved_decision(decision)
    write_back = decision.get("write_back") or {
        "status": "mocked_success",
        "target": "demo_write_back",
        "idempotency_key": f"writeback:{decision_id}",
        "applied_at": learning_event["created_at"],
    }
    updated = decision_store.annotate(
        decision_id,
        outcome=learning_event["outcome"],
        learning_event=learning_event,
        write_back=write_back,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"decision": updated, "learning_event": learning_event}


@app.post("/decisions/{decision_id}/reject")
def reject_decision(decision_id: str) -> dict[str, object]:
    decision = decision_store.reject(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"decision": decision, "learning_event": None}
