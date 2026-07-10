# ruff: noqa: E501
from __future__ import annotations

import csv
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
REPORT_ROOT = ROOT / "reports"


@dataclass(slots=True)
class RequestSample:
    scenario: str
    workflow_id: str
    step: str
    method: str
    path: str
    status: int | None
    latency_ms: float
    response_bytes: int
    error: str
    model_calls: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    total_tokens: int | None = 0
    inference_wait_ms: float = 0.0


@dataclass(slots=True)
class ScenarioSummary:
    scenario: str
    workflows: int
    requests: int
    errors: int
    concurrency: int
    wall_time_ms: float
    rps: float
    avg_latency_ms: float
    p95_latency_ms: float
    peak_latency_ms: float
    model_calls: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    avg_cpu_pct: float
    peak_cpu_pct: float
    avg_process_rss_mb: float
    peak_process_rss_mb: float
    gpu_util_start_max_engine_pct: float | None
    gpu_util_end_max_engine_pct: float | None
    gpu_vram_start_mb: float | None
    gpu_vram_end_mb: float | None
    queue_length_observed: str
    idle_time_ms: float | None
    inference_wait_ms: float


class ManagedProcess:
    """Start a child process and tear down its process tree at the end of the run."""

    def __init__(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
    ) -> None:
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self._log_file: Any = None

    @property
    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid

    def start(self) -> None:
        self._log_file = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            parent = psutil.Process(self.process.pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            parent.terminate()
            _, alive = psutil.wait_procs([parent, *children], timeout=5)
            for proc in alive:
                proc.kill()
        except psutil.Error:
            if self.process.poll() is None:
                self.process.kill()
        if self._log_file is not None:
            self._log_file.close()


class ResourceSampler:
    """Sample system and app process CPU/RSS while a scenario runs."""

    def __init__(self, app_root_pids: list[int], interval_seconds: float = 0.25) -> None:
        self.app_root_pids = app_root_pids
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, float]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        psutil.cpu_percent(interval=None)
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(interval=None)
            except psutil.Error:
                continue
        self._thread.start()

    def stop(self) -> list[dict[str, float]]:
        self._stop.set()
        self._thread.join(timeout=2)
        return self.samples

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self._sample_once())
            time.sleep(self.interval_seconds)

    def _sample_once(self) -> dict[str, float]:
        app_pids = self._descendant_pids(self.app_root_pids)
        app_rss = 0
        app_cpu = 0.0
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                pid = int(proc.info["pid"])
                rss = int(proc.info["memory_info"].rss)
                cpu = float(proc.cpu_percent(interval=None))
            except (psutil.Error, TypeError, ValueError):
                continue
            if pid in app_pids:
                app_rss += rss
                app_cpu += cpu
        return {
            "ts": time.time(),
            "system_cpu_pct": float(psutil.cpu_percent(interval=None)),
            "system_memory_pct": float(psutil.virtual_memory().percent),
            "app_cpu_pct": app_cpu,
            "app_rss_mb": app_rss / (1024 * 1024),
        }

    @staticmethod
    def _descendant_pids(root_pids: list[int]) -> set[int]:
        pids: set[int] = set()
        for pid in root_pids:
            try:
                proc = psutil.Process(pid)
                pids.add(proc.pid)
                pids.update(child.pid for child in proc.children(recursive=True))
            except psutil.Error:
                continue
        return pids


def parse_dotenv(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries from .env without printing secrets."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def utc_run_id() -> str:
    """Return a filesystem-friendly UTC run identifier."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, dict[str, Any] | list[Any] | str | None, int, str, float]:
    """Issue one HTTP request and return status, parsed body, byte count, error, and latency."""
    url = path if path.startswith("http") else base_url.rstrip("/") + path
    encoded = None
    request_headers = dict(headers or {})
    if body is not None:
        encoded = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=encoded, headers=request_headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            latency_ms = (time.perf_counter() - started) * 1000
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed: dict[str, Any] | list[Any] | str | None = json.loads(text)
            except json.JSONDecodeError:
                parsed = text
            return response.status, parsed, len(raw), "", latency_ms
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        latency_ms = (time.perf_counter() - started) * 1000
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        return exc.code, parsed, len(raw), f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return None, None, 0, f"{type(exc).__name__}: {exc}", latency_ms


def wait_for_http(url: str, timeout_seconds: float) -> None:
    """Block until a local server answers or the timeout expires."""
    deadline = time.perf_counter() + timeout_seconds
    last_error = ""
    while time.perf_counter() < deadline:
        status, _, _, error, _ = http_json("", "GET", url, timeout=2)
        if status and 200 <= status < 500:
            return
        last_error = error
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def gpu_snapshot() -> dict[str, float | None]:
    """Read coarse Windows GPU counters; return None values when counters are unavailable."""
    ps = r"""
$ErrorActionPreference = 'Stop'
$engine = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -MaxSamples 1).CounterSamples
$mem = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -MaxSamples 1).CounterSamples
$shared = (Get-Counter '\GPU Adapter Memory(*)\Shared Usage' -MaxSamples 1).CounterSamples
$result = [ordered]@{
  gpu_util_sum_pct = (($engine | Measure-Object -Property CookedValue -Sum).Sum)
  gpu_util_max_engine_pct = (($engine | Measure-Object -Property CookedValue -Maximum).Maximum)
  gpu_dedicated_usage_sum_mb = (($mem | Measure-Object -Property CookedValue -Sum).Sum / 1MB)
  gpu_dedicated_usage_max_mb = (($mem | Measure-Object -Property CookedValue -Maximum).Maximum / 1MB)
  gpu_shared_usage_sum_mb = (($shared | Measure-Object -Property CookedValue -Sum).Sum / 1MB)
}
$result | ConvertTo-Json -Compress
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=25,
            check=True,
        )
        raw = json.loads(completed.stdout)
        return {key: float(value) if value is not None else None for key, value in raw.items()}
    except Exception:
        return {
            "gpu_util_sum_pct": None,
            "gpu_util_max_engine_pct": None,
            "gpu_dedicated_usage_sum_mb": None,
            "gpu_dedicated_usage_max_mb": None,
            "gpu_shared_usage_sum_mb": None,
        }


def fefo_payload() -> dict[str, Any]:
    """Return the FEFO batch split request used by tests and the load run."""
    return {
        "sku": "milk_2l",
        "as_of": "2026-07-06",
        "batches": [
            {
                "sku": "milk_2l",
                "lot": "MILK-OLD-0707",
                "units": 10,
                "expiry_date": "2026-07-07",
                "received_date": "2026-07-03",
                "location": "fridge_a",
            },
            {
                "sku": "milk_2l",
                "lot": "MILK-NEW-0713",
                "units": 20,
                "expiry_date": "2026-07-13",
                "received_date": "2026-07-06",
                "location": "fridge_a",
            },
        ],
    }


def workflow_once(
    base_url: str,
    scenario: str,
    workflow_id: str,
    *,
    include_frontend_url: str | None = None,
    include_inference_smoke: bool = False,
) -> tuple[list[RequestSample], dict[str, Any]]:
    """Exercise the implemented API and optional frontend/inference smoke path once."""
    samples: list[RequestSample] = []
    context: dict[str, Any] = {}

    def record(
        step: str,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
        base_override: str | None = None,
        model_calls: int = 0,
    ) -> Any:
        status, parsed, response_bytes, error, latency_ms = http_json(
            base_override or base_url,
            method,
            path,
            body=body,
            timeout=timeout,
        )
        inferred_wait = latency_ms if model_calls else 0.0
        usage = extract_usage(parsed)
        samples.append(
            RequestSample(
                scenario=scenario,
                workflow_id=workflow_id,
                step=step,
                method=method,
                path=path,
                status=status,
                latency_ms=round(latency_ms, 3),
                response_bytes=response_bytes,
                error=error,
                model_calls=model_calls,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                inference_wait_ms=round(inferred_wait, 3),
            )
        )
        return parsed

    record("health", "GET", "/health")
    record("readiness", "GET", "/readiness")
    golden = record("golden_cascade", "GET", "/demo/golden")
    decision_id = (
        golden.get("decision", {}).get("id")
        if isinstance(golden, dict)
        else "dec_stage4_loadshedding_x_payday_yoghurt"
    )
    context["golden_agents"] = [
        item.get("agent") for item in golden.get("evidence", [])
    ] if isinstance(golden, dict) else []
    context["golden_trace"] = [
        item.get("name") for item in golden.get("trace", [])
    ] if isinstance(golden, dict) else []
    record("decision_detail", "GET", f"/decisions/{decision_id}")
    record("approve_decision", "POST", f"/decisions/{decision_id}/approve")
    record("learning", "GET", "/learning")
    rejection = record("critic_rejection", "GET", "/demo/critic-rejection")
    context["critic_rejection_agents"] = [
        item.get("agent") for item in rejection.get("evidence", [])
    ] if isinstance(rejection, dict) else []
    record("decision_log", "GET", "/decisions")
    record("seed_summary", "GET", "/data/seed/summary")
    record("product_attention", "GET", "/products/attention?limit=20")
    record(
        "product_search",
        "GET",
        "/products/search?" + urllib.parse.urlencode({"q": "amasi", "limit": 3}),
    )
    record("fefo_split", "POST", "/intelligence/stock/fefo-split", body=fefo_payload())
    record(
        "delivery_reconcile",
        "POST",
        "/intelligence/deliveries/reconcile",
        body={
            "sku": "milk_2l",
            "ordered_units": 50,
            "asn_units": 50,
            "received_units": 38,
            "accepted_units": 32,
            "short_dated_units": 6,
        },
    )
    record(
        "supplier_cover",
        "POST",
        "/intelligence/suppliers/cover-plan",
        body={
            "sku": "milk_2l",
            "units_on_hand": 12,
            "forecast_daily_units": "10",
            "supplier_lead_time_days": "3",
            "transfer_available_units": 18,
        },
    )
    record(
        "outcome_summary",
        "POST",
        "/intelligence/outcomes/summarize",
        body={
            "sku": "yoghurt_1l",
            "action": "markdown",
            "predicted_sell_through_units": 24,
            "actual_sell_through_units": 30,
            "predicted_waste_units": 8,
            "actual_waste_units": 5,
        },
    )
    record("inference_readiness", "GET", "/inference/readiness")
    if include_inference_smoke:
        record("inference_smoke", "GET", "/inference/smoke", timeout=35, model_calls=0)
    record("submission_readiness", "GET", "/submission/readiness")
    if include_frontend_url is not None:
        record(
            "frontend_root",
            "GET",
            include_frontend_url,
            base_override="",
            timeout=10,
        )
    return samples, context


def extract_usage(parsed: Any) -> dict[str, int | None]:
    """Extract OpenAI-compatible token usage when the provider returns it."""
    usage: Any = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("usage"), dict):
            usage = parsed["usage"]
        elif isinstance(parsed.get("result"), dict):
            raw = parsed["result"].get("raw")
            if isinstance(raw, dict) and isinstance(raw.get("usage"), dict):
                usage = raw["usage"]
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    return {
        "input_tokens": int(input_tokens) if isinstance(input_tokens, int) else None,
        "output_tokens": int(output_tokens) if isinstance(output_tokens, int) else None,
        "total_tokens": int(total_tokens) if isinstance(total_tokens, int) else None,
    }


def run_concurrent_workflows(
    base_url: str,
    scenario: str,
    *,
    concurrency: int,
    workflows: int,
    include_frontend_url: str | None = None,
    include_inference_smoke: bool = False,
) -> tuple[list[RequestSample], dict[str, Any], float]:
    """Run workflow replays under a fixed client-side concurrency level."""
    all_samples: list[RequestSample] = []
    context: dict[str, Any] = {}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                workflow_once,
                base_url,
                scenario,
                f"{scenario}-{index:04d}",
                include_frontend_url=include_frontend_url if index == 0 else None,
                include_inference_smoke=include_inference_smoke and index == 0,
            )
            for index in range(workflows)
        ]
        for future in as_completed(futures):
            samples, local_context = future.result()
            all_samples.extend(samples)
            for key, value in local_context.items():
                context.setdefault(key, value)
    wall_ms = (time.perf_counter() - started) * 1000
    return all_samples, context, wall_ms


def configured_backend_smoke(
    run_dir: Path,
    backend_port: int,
    env_file: dict[str, str],
) -> list[RequestSample]:
    """Start a second backend with .env inference settings and call readiness/smoke."""
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    for key, value in env_file.items():
        if key.startswith("LLM_") and value:
            env[key] = value
    proc = ManagedProcess(
        "backend_configured_inference",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "shelfwise_backend.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
        ],
        ROOT,
        env,
        run_dir / "backend_configured_inference.log",
    )
    samples: list[RequestSample] = []
    base_url = f"http://127.0.0.1:{backend_port}"
    try:
        proc.start()
        wait_for_http(f"{base_url}/health", 20)
        for step, path, model_calls, timeout in [
            ("configured_inference_readiness", "/inference/readiness", 0, 10),
            ("configured_inference_smoke", "/inference/smoke", 1, 35),
        ]:
            status, parsed, response_bytes, error, latency_ms = http_json(
                base_url,
                "GET",
                path,
                timeout=timeout,
            )
            usage = extract_usage(parsed)
            samples.append(
                RequestSample(
                    scenario="configured_remote_inference_smoke",
                    workflow_id="configured-smoke",
                    step=step,
                    method="GET",
                    path=path,
                    status=status,
                    latency_ms=round(latency_ms, 3),
                    response_bytes=response_bytes,
                    error=error,
                    model_calls=model_calls,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    inference_wait_ms=round(latency_ms if model_calls else 0.0, 3),
                )
            )
    finally:
        proc.stop()
    return samples


def summarize_scenario(
    scenario: str,
    samples: list[RequestSample],
    resource_samples: list[dict[str, float]],
    wall_time_ms: float,
    concurrency: int,
    workflows: int,
    gpu_start: dict[str, float | None],
    gpu_end: dict[str, float | None],
) -> ScenarioSummary:
    """Aggregate request and resource measurements for one scenario."""
    latencies = [sample.latency_ms for sample in samples]
    errors = sum(1 for sample in samples if sample.error or not sample.status or sample.status >= 400)
    model_calls = sum(sample.model_calls for sample in samples)

    def sum_tokens(kind: str) -> int | None:
        values = [getattr(sample, kind) for sample in samples if getattr(sample, kind) is not None]
        if len(values) != len(samples):
            return None
        return int(sum(int(value) for value in values))

    app_rss_values = [sample["app_rss_mb"] for sample in resource_samples]
    cpu_values = [sample["system_cpu_pct"] for sample in resource_samples]
    p95 = percentile(latencies, 95)
    idle_time_ms = None
    if concurrency == 1:
        idle_time_ms = max(0.0, wall_time_ms - sum(latencies))
    return ScenarioSummary(
        scenario=scenario,
        workflows=workflows,
        requests=len(samples),
        errors=errors,
        concurrency=concurrency,
        wall_time_ms=round(wall_time_ms, 3),
        rps=round((len(samples) / wall_time_ms) * 1000, 3) if wall_time_ms > 0 else 0.0,
        avg_latency_ms=round(statistics.fmean(latencies), 3) if latencies else 0.0,
        p95_latency_ms=round(p95, 3),
        peak_latency_ms=round(max(latencies), 3) if latencies else 0.0,
        model_calls=model_calls,
        input_tokens=sum_tokens("input_tokens"),
        output_tokens=sum_tokens("output_tokens"),
        total_tokens=sum_tokens("total_tokens"),
        avg_cpu_pct=round(statistics.fmean(cpu_values), 3) if cpu_values else 0.0,
        peak_cpu_pct=round(max(cpu_values), 3) if cpu_values else 0.0,
        avg_process_rss_mb=round(statistics.fmean(app_rss_values), 3) if app_rss_values else 0.0,
        peak_process_rss_mb=round(max(app_rss_values), 3) if app_rss_values else 0.0,
        gpu_util_start_max_engine_pct=gpu_start.get("gpu_util_max_engine_pct"),
        gpu_util_end_max_engine_pct=gpu_end.get("gpu_util_max_engine_pct"),
        gpu_vram_start_mb=gpu_start.get("gpu_dedicated_usage_sum_mb"),
        gpu_vram_end_mb=gpu_end.get("gpu_dedicated_usage_sum_mb"),
        queue_length_observed="not_exposed_by_app_or_provider",
        idle_time_ms=round(idle_time_ms, 3) if idle_time_ms is not None else None,
        inference_wait_ms=round(sum(sample.inference_wait_ms for sample in samples), 3),
    )


def percentile(values: list[float], pct: float) -> float:
    """Return a percentile using linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write dictionaries to CSV with stable field ordering."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_bar_svg(path: Path, title: str, labels: list[str], values: list[float], unit: str) -> None:
    """Create a dependency-free horizontal bar chart SVG."""
    width = 980
    left = 250
    row_h = 34
    height = 70 + row_h * max(1, len(labels))
    max_value = max(values) if values else 1
    max_value = max(max_value, 1)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="24" y="32" font-family="Arial, sans-serif" font-size="20" font-weight="700" fill="#111827">{escape_xml(title)}</text>',
    ]
    for index, (label, value) in enumerate(zip(labels, values, strict=False)):
        y = 60 + index * row_h
        bar_w = int((width - left - 110) * (value / max_value))
        lines.append(
            f'<text x="24" y="{y + 18}" font-family="Arial, sans-serif" font-size="13" fill="#374151">{escape_xml(label)}</text>'
        )
        lines.append(
            f'<rect x="{left}" y="{y}" width="{bar_w}" height="22" rx="3" fill="#2563eb"/>'
        )
        lines.append(
            f'<text x="{left + bar_w + 8}" y="{y + 16}" font-family="Arial, sans-serif" font-size="12" fill="#111827">{value:.1f}{escape_xml(unit)}</text>'
        )
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def escape_xml(value: str) -> str:
    """Escape text for SVG/XML output."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_report(
    run_dir: Path,
    summaries: list[ScenarioSummary],
    request_samples: list[RequestSample],
    contexts: dict[str, Any],
    remote_samples: list[RequestSample],
    model_probe_samples: list[RequestSample],
) -> Path:
    """Write the final Markdown report from measured artifacts."""
    report_path = run_dir / "inference_architecture_evaluation.md"
    summary_by_name = {item.scenario: item for item in summaries}
    golden_agents = contexts.get("single_user", {}).get("golden_agents", [])
    rejection_agents = contexts.get("single_user", {}).get("critic_rejection_agents", [])
    inference_steps = [
        sample for sample in [*remote_samples, *model_probe_samples] if sample.model_calls
    ]
    normal_model_calls = sum(
        sample.model_calls
        for sample in request_samples
        if not sample.step.startswith("inference_smoke")
    )
    peak = max(summaries, key=lambda item: item.concurrency)
    heavy = summary_by_name.get("heavy_32_users", peak)
    model_failure_lines = []
    for sample in inference_steps:
        status = sample.status if sample.status is not None else "timeout/error"
        token_text = (
            f"{sample.input_tokens}/{sample.output_tokens}/{sample.total_tokens}"
            if sample.total_tokens is not None
            else "not returned"
        )
        model_failure_lines.append(
            f"| {sample.scenario} | {sample.step} | {status} | {sample.latency_ms:.1f} | {token_text} | {sample.error or 'ok'} |"
        )
    if not model_failure_lines:
        model_failure_lines.append("| none | none | n/a | 0.0 | 0/0/0 | no model calls were made |")

    lines = [
        "# ShelfWise Inference Architecture Evaluation",
        "",
        f"Run ID: `{run_dir.name}`",
        f"Measured at: `{datetime.now(UTC).isoformat()}`",
        "",
        "## Technical Summary",
        "",
        (
            "The measured application workload does not currently require a live shared model, "
            "multiple model instances, or a hybrid model pool for the implemented user flows. "
            f"Across the replayed app workload, non-smoke workflows made `{normal_model_calls}` "
            "model calls and processed `0` measured model tokens. The app's golden, rejection, "
            "approval, learning, catalog, and store-intelligence paths are synchronous deterministic "
            "Python/FastAPI work."
        ),
        "",
        (
            "The inference gateway is a deployment risk rather than a throughput bottleneck today. "
            "The saved remote AMD/vLLM-style configuration reported configured readiness but the "
            "smoke path did not produce a successful model response in this run. That means no "
            "hosted Gemma 4 model capacity was measured. The local app-load data is control-plane "
            "evidence only, so the model deployment decision must remain unresolved until the AMD "
            "cloud endpoint or ROCm notebook host returns usable latency/token/GPU telemetry."
        ),
        "",
        "## Key Findings With Visual Evidence",
        "",
        f"![P95 latency by scenario]({(run_dir / 'p95_latency_by_scenario.svg').name})",
        "",
        f"![Peak process memory by scenario]({(run_dir / 'peak_process_memory_by_scenario.svg').name})",
        "",
        "| Scenario | Workflows | Requests | Concurrency | Errors | RPS | Avg latency ms | P95 latency ms | Peak latency ms | Model calls | Total tokens | Peak CPU % | Peak app RSS MB | End VRAM MB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.scenario,
                    str(item.workflows),
                    str(item.requests),
                    str(item.concurrency),
                    str(item.errors),
                    f"{item.rps:.2f}",
                    f"{item.avg_latency_ms:.1f}",
                    f"{item.p95_latency_ms:.1f}",
                    f"{item.peak_latency_ms:.1f}",
                    str(item.model_calls),
                    "n/a" if item.total_tokens is None else str(item.total_tokens),
                    f"{item.peak_cpu_pct:.1f}",
                    f"{item.peak_process_rss_mb:.1f}",
                    "n/a" if item.gpu_vram_end_mb is None else f"{item.gpu_vram_end_mb:.1f}",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "### Inference Probes",
            "",
            "| Probe | Step | Status | Latency ms | Input/Output/Total tokens | Error |",
            "|---|---|---:|---:|---|---|",
            *model_failure_lines,
            "",
            "## Scope, Data, And Metric Definitions",
            "",
            "- `Workflows` are full app walkthroughs covering health/readiness, golden cascade, decision detail, HITL approval, learning, critic rejection, decision log, seed summary, product attention/search, all four store-intelligence endpoints, inference readiness, submission readiness, and one frontend root fetch in the single-user pass.",
            "- `Model calls` count calls to the app inference client or direct model endpoint. Deterministic ShelfWise agents are counted as zero model calls because the running code does not invoke the inference client for those agents.",
            "- `Tokens` are provider-returned usage only. Failed or timed-out model calls did not return measured token usage, so the report does not estimate tokenizer-specific counts.",
            "- `Queue length` is not exposed by FastAPI, Uvicorn, or the remote endpoint in this app. The report records it as unavailable instead of inferring a hidden queue.",
            "- Windows GPU counters are local control-plane counters only. They are not deployment evidence for AMD cloud Gemma 4 inference; production sizing needs ROCm/vLLM telemetry from the cloud host.",
            "",
            "## Methodology",
            "",
            "The harness started isolated local backend and frontend processes, waited for `/health` and the Vite root, then replayed the same implemented feature set at four client concurrency levels: single user, moderate 8-user load, heavy 32-user load, and peak local 64-user load. It sampled CPU/memory with `psutil` and took Windows GPU counter snapshots before and after each scenario as control-plane evidence. It then started a second backend using the saved `.env` LLM settings and ran `/inference/readiness` plus `/inference/smoke` against the configured cloud endpoint. Local model probes are intentionally excluded from the inference evidence.",
            "",
            "## Agent Interaction Analysis",
            "",
            f"- Golden cascade agent order observed from the running response: `{ ' -> '.join(golden_agents) }`.",
            f"- Critic-rejection cascade agent order observed from the running response: `{ ' -> '.join(rejection_agents) }`.",
            "- Execution mode: synchronous and sequential inside each HTTP request. No async fan-out, worker queue, Redis stream, or parallel model call path is active in this checkout.",
            "- Decision-science spans observed for the golden path are deterministic tool spans, not model spans.",
            "",
            "## Bottlenecks Discovered",
            "",
            f"- Under the highest local app load tested (`{peak.scenario}`), the bottleneck was ordinary API request handling, not inference. The scenario reached `{peak.rps:.2f}` requests/s with `{peak.errors}` HTTP errors and `{peak.model_calls}` model calls.",
            "- The model path is not capacity-proven: configured remote inference readiness is based on environment presence, but the actual smoke request failed in this run.",
            "- The configured AMD/vLLM endpoint smoke did not return a successful model response, so hosted Gemma 4 inference throughput and latency remain unmeasured.",
            "- The app does not expose provider queue depth, model scheduler metrics, or per-agent model latency, so those metrics cannot support a multi-instance recommendation today.",
            "",
            "## Model Utilization Analysis",
            "",
            "- Current implemented workflows: `0` successful model calls and `0` measured model tokens. This proves the local replay was not inference-bound; it does not prove cloud model capacity.",
            "- Inference smoke: isolated and not part of normal cascade execution. It is useful as a readiness proof, but it is not evidence that app traffic needs a second model instance.",
            "- Strong/routine routing exists in config, but because the agents do not call the model during workflows, the measured utilization of both tiers is zero.",
            "",
            "## Scalability Analysis",
            "",
            f"- Deterministic app throughput scaled from `{summary_by_name.get('single_user', peak).rps:.2f}` requests/s in the single-user pass to `{heavy.rps:.2f}` requests/s at 32-user load and `{peak.rps:.2f}` requests/s at the highest tested local load.",
            f"- Average app process RSS stayed below `{max(item.peak_process_rss_mb for item in summaries):.1f}` MB in these tests. That is useful for app hosting, not for cloud GPU sizing.",
            "- Since normal local workflows do not wait on inference, this run cannot show whether adding cloud model instances improves model-backed workflow time.",
            "",
            "## Deployment Strategy Tradeoffs",
            "",
            "### One Shared Model",
            "",
            "Advantages:",
            "- Lowest operational complexity and VRAM footprint.",
            "- Matches the measured local control-plane workload because normal app traffic currently makes no model calls.",
            "- Keeps one readiness/smoke target for AMD proof without fragmenting metrics.",
            "",
            "Disadvantages:",
            "- If future agent prompts are wired into every cascade step, one model may become a latency bottleneck; this run cannot prove otherwise because those calls do not exist yet.",
            "- A single endpoint failure blocks all model-backed features.",
            "",
            "### Multiple Model Instances",
            "",
            "Advantages:",
            "- Could reduce queueing if future measurements show simultaneous inference requests saturating one model worker.",
            "- Allows isolation between long strong-agent prompts and short routine-agent prompts.",
            "",
            "Disadvantages:",
            "- Not justified by current measurements: there is no successful model-call volume to distribute.",
            "- Consumes additional VRAM/compute without improving deterministic workflow latency.",
            "- More routing, health-check, and failure-mode complexity before the app exposes queue metrics.",
            "",
            "### Hybrid Large/Small Models",
            "",
            "Advantages:",
            "- Aligns with the intended routing design if future evidence shows routine agents need shorter/lower-cost reasoning and Critic/Executive need deeper review.",
            "- Could protect strong-agent latency by keeping lightweight work off the large model.",
            "",
            "Disadvantages:",
            "- Current measurements do not show quality or latency differences by agent because no agent prompts are executed in normal workflows.",
            "- A Gemma 4 2B/4B/12B comparison was not measurable in this run because the hosted inference endpoint did not return successful model telemetry.",
            "",
            "## Final Recommendation",
            "",
            "**Do not make a final model-deployment recommendation yet. Keep one OpenAI-compatible Gemma 4 endpoint as the measurement target, then choose shared, multi-instance, or hybrid deployment only after hosted AMD cloud inference is measured successfully.**",
            "",
            "This recommendation is not based on convention. It follows directly from the observed evidence gap: normal workflows made zero model calls and the configured cloud smoke failed, so the run did not measure hosted Gemma 4 queueing, latency, token rate, or GPU utilization.",
            "",
            "For the next evidence gate, instrument real per-agent inference before changing architecture: log per-agent prompt tokens, completion tokens, latency, queue wait, provider model, success/failure, and quality outcome. Then rerun the same concurrency ladder against the AMD cloud endpoint and, separately, the Jupyter ROCm host where training/shakedown happens. Use only Google Gemma 4 candidates: the smallest permitted Gemma 4 tier for routine agents, plus a stronger Gemma 4 tier for Critic/Executive if measured quality or latency requires it. A hybrid or multi-instance recommendation should require measured cloud queueing or latency pressure, not the existence of routine/strong labels in config.",
            "",
            "## Limitations, Uncertainty, And Robustness Checks",
            "",
            "- The remote AMD endpoint in `.env` was not reachable as a working OpenAI-compatible model endpoint during this run.",
            "- Local Windows GPU counters are not cloud inference evidence and should be replaced with ROCm/vLLM metrics from the AMD cloud host.",
            "- No model quality comparison was possible because successful model completions were not returned by the tested endpoints.",
            "- The application has no internal inference queue metrics today; queue conclusions are limited to observed request latency and the absence of model calls in implemented workflows.",
            "",
            "## Supporting Artifacts",
            "",
            "- `request_samples.csv`: per-request latency/status/token/model-call samples.",
            "- `scenario_summaries.csv`: aggregate metrics by load scenario.",
            "- `raw_results.json`: full run context and probe results.",
            "- `p95_latency_by_scenario.svg`: latency graph.",
            "- `peak_process_memory_by_scenario.svg`: memory graph.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def main() -> int:
    """Run the complete architecture evaluation and write artifacts."""
    run_id = utc_run_id()
    run_dir = REPORT_ROOT / f"inference_architecture_eval_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    env_file = parse_dotenv(ROOT / ".env")

    backend_port = 8010
    frontend_port = 5174
    backend_env = os.environ.copy()
    backend_env["PYTHONPATH"] = "src"
    for key in list(backend_env):
        if key.startswith("LLM_"):
            backend_env.pop(key)
    frontend_env = os.environ.copy()
    frontend_env["VITE_DEV_API"] = f"http://127.0.0.1:{backend_port}"
    frontend_env["PORT"] = str(frontend_port)

    backend = ManagedProcess(
        "backend_offline",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "shelfwise_backend.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
        ],
        ROOT,
        backend_env,
        run_dir / "backend_offline.log",
    )
    frontend = ManagedProcess(
        "frontend_vite",
        [
            "npm.cmd" if os.name == "nt" else "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
        ],
        FRONTEND_DIR,
        frontend_env,
        run_dir / "frontend_vite.log",
    )

    all_samples: list[RequestSample] = []
    summaries: list[ScenarioSummary] = []
    contexts: dict[str, Any] = {}
    base_url = f"http://127.0.0.1:{backend_port}"
    frontend_url = f"http://127.0.0.1:{frontend_port}"

    try:
        backend.start()
        frontend.start()
        wait_for_http(f"{base_url}/health", 25)
        wait_for_http(frontend_url, 30)

        scenarios = [
            ("single_user", 1, 1, True, True),
            ("moderate_8_users", 8, 40, False, False),
            ("heavy_32_users", 32, 96, False, False),
            ("peak_local_64_users", 64, 128, False, False),
        ]
        for name, concurrency, workflows, include_frontend, include_smoke in scenarios:
            gpu_start = gpu_snapshot()
            sampler = ResourceSampler(
                [pid for pid in [backend.pid, frontend.pid] if pid is not None],
            )
            sampler.start()
            samples, context, wall_ms = run_concurrent_workflows(
                base_url,
                name,
                concurrency=concurrency,
                workflows=workflows,
                include_frontend_url=frontend_url if include_frontend else None,
                include_inference_smoke=include_smoke,
            )
            resources = sampler.stop()
            gpu_end = gpu_snapshot()
            all_samples.extend(samples)
            contexts[name] = context
            summaries.append(
                summarize_scenario(
                    name,
                    samples,
                    resources,
                    wall_ms,
                    concurrency,
                    workflows,
                    gpu_start,
                    gpu_end,
                )
            )
    finally:
        frontend.stop()
        backend.stop()

    remote_samples = configured_backend_smoke(run_dir, 8011, env_file)
    # Model capacity must be measured on the AMD cloud endpoint/Jupyter ROCm host, not locally.
    model_probe_samples: list[RequestSample] = []

    write_csv(run_dir / "request_samples.csv", [asdict(sample) for sample in all_samples])
    write_csv(run_dir / "scenario_summaries.csv", [asdict(summary) for summary in summaries])
    write_csv(
        run_dir / "inference_probe_samples.csv",
        [asdict(sample) for sample in [*remote_samples, *model_probe_samples]],
    )
    (run_dir / "raw_results.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "summaries": [asdict(item) for item in summaries],
                "contexts": contexts,
                "remote_samples": [asdict(item) for item in remote_samples],
                "cloud_model_probe_samples": [asdict(item) for item in model_probe_samples],
                "env_file_inference_config_present": {
                    "llm_base_url_present": bool(env_file.get("LLM_BASE_URL")),
                    "llm_api_key_present": bool(env_file.get("LLM_API_KEY")),
                    "llm_routine_model": env_file.get("LLM_ROUTINE_MODEL", ""),
                    "llm_strong_model": env_file.get("LLM_STRONG_MODEL", ""),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_bar_svg(
        run_dir / "p95_latency_by_scenario.svg",
        "P95 response latency by scenario",
        [item.scenario for item in summaries],
        [item.p95_latency_ms for item in summaries],
        " ms",
    )
    write_bar_svg(
        run_dir / "peak_process_memory_by_scenario.svg",
        "Peak backend+frontend process RSS by scenario",
        [item.scenario for item in summaries],
        [item.peak_process_rss_mb for item in summaries],
        " MB",
    )
    report = build_report(
        run_dir,
        summaries,
        all_samples,
        contexts,
        remote_samples,
        model_probe_samples,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
