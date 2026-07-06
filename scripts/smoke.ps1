$ErrorActionPreference = "Stop"
$env:PYTHONPATH = "src"
python -m pytest -q
@'
from fastapi.testclient import TestClient

from shelfwise_backend import run_critic_rejection_cascade, run_golden_cascade
from shelfwise_backend.app import app

golden = run_golden_cascade()
assert golden["decision"]["status"] == "pending"
assert golden["decision"]["action"]["type"] == "apply_markdown"
assert len(golden["evidence"]) == 7

rejection = run_critic_rejection_cascade()
assert rejection["decision"]["status"] == "rejected"
assert rejection["decision"]["action"]["type"] == "monitor"
assert rejection["decision"]["critic_verdict"] == "rejected"

client = TestClient(app)
demo = client.get("/demo/golden")
demo.raise_for_status()
decision = demo.json()["decision"]
listed = client.get("/decisions")
listed.raise_for_status()
assert any(item["id"] == decision["id"] for item in listed.json()["decisions"])

print("smoke ok: golden pending, critic rejected, decision log populated")
'@ | python -
