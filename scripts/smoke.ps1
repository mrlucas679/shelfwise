$ErrorActionPreference = "Stop"
$env:PYTHONPATH = "src"

python -m pytest -q
python scripts/smoke.py
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
approve = client.post(f"/decisions/{decision['id']}/approve")
approve.raise_for_status()
assert approve.json()["decision"]["status"] == "approved"
repeated = client.get("/demo/golden")
repeated.raise_for_status()
assert repeated.json()["decision"]["id"] == decision["id"]
assert repeated.json()["decision"]["status"] == "approved"
listed = client.get("/decisions")
listed.raise_for_status()
listed_decision = next(item for item in listed.json()["decisions"] if item["id"] == decision["id"])
assert listed_decision["status"] == "approved"
attention = client.get("/products/attention")
attention.raise_for_status()
assert attention.json()["sell_first"]
search = client.get("/products/search", params={"q": "amasi", "limit": 3})
search.raise_for_status()
product = search.json()["products"][0]
assert product["name"] == "Amasi 2L"
assert product["fefo_batches"][0]["lot"] == "AMASI-OLD-0707"
inference_ready = client.get("/inference/readiness")
inference_ready.raise_for_status()
assert inference_ready.json()["inference"]["timeout_seconds"] < 30
submission_ready = client.get("/submission/readiness")
submission_ready.raise_for_status()
assert submission_ready.json()["track"] == "Track 3: Unicorn"

print("smoke ok: terminal decision preserved, product search wired, GPU preflight exposed")
'@ | python -
python -m shelfwise_eval
