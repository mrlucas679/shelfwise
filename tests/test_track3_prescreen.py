from __future__ import annotations

import json

from scripts.track3_prescreen import ProbeResult, run_prescreen


def test_track3_prescreen_records_amd_and_unseen_chat_proof(monkeypatch) -> None:
    responses = [
        ({}, json.dumps({"ok": True}).encode(), ProbeResult("/health", 120, 200)),
        (
            {},
            json.dumps(
                {
                    "ready_for_amd_demo": True,
                    "inference": {
                        "provider": "vllm_mi300x",
                        "routine_model": "google/gemma-4-E4B-it",
                        "strong_model": "google/gemma-4-31B-it",
                    },
                }
            ).encode(),
            ProbeResult("/inference/readiness", 220, 200),
        ),
        ({"Set-Cookie": "session=test; Path=/"}, b"{}", ProbeResult("/auth/session", 80, 200)),
        (
            {
                "X-ShelfWise-Provider": "vllm_mi300x",
                "X-ShelfWise-Answer-Source": "model",
                "X-ShelfWise-Replayed": "false",
                "X-ShelfWise-Correlation-ID": "chat-one",
                "X-ShelfWise-Model": "google/gemma-4-E4B-it",
            },
            b"Stock is available.",
            ProbeResult("/chat", 600, 200),
        ),
        (
            {
                "X-ShelfWise-Provider": "vllm_mi300x",
                "X-ShelfWise-Answer-Source": "model",
                "X-ShelfWise-Replayed": "false",
                "X-ShelfWise-Correlation-ID": "chat-two",
                "X-ShelfWise-Model": "google/gemma-4-E4B-it",
            },
            b"The cold-chain alert needs review.",
            ProbeResult("/chat", 700, 200),
        ),
    ]

    def fake_request(*args, **kwargs):
        del args, kwargs
        return responses.pop(0)

    monkeypatch.setattr("scripts.track3_prescreen._request", fake_request)

    receipt = run_prescreen("https://demo.example", startup_deadline=60, request_deadline=29)

    assert receipt["verdict"] == "PASS"
    assert len(receipt["chat"]) == 2
    assert [item["correlation_id"] for item in receipt["chat"]] == ["chat-one", "chat-two"]
