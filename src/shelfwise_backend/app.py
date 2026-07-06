from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shelfwise_action import DecisionStore
from shelfwise_inference import OpenAICompatibleInferenceClient, load_inference_config

from .cascade import run_golden_cascade

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
decision_store = DecisionStore()


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
    return {
        "ready": True,
        "checks": {
            "backend": "ok",
            "golden_cascade": "ok",
            "hitl": "ok",
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
    return {"decision": decision}


@app.post("/decisions/{decision_id}/reject")
def reject_decision(decision_id: str) -> dict[str, object]:
    decision = decision_store.reject(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {"decision": decision}
