from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .adapters import VllmMetricsClient
from .models import EndpointSpec, EvidenceScope, TelemetrySample

_GPU_UTIL_KEYS = (
    "gfx",
    "gfx_percent",
    "gfx_activity",
    "gpu_util",
    "gpu_utilization",
    "gpu_activity",
)
_VRAM_USED_KEYS = (
    "vram_used",
    "vram_used_mb",
    "vram_usage_used",
    "memory_vram_used",
)
_DEVICE_KEYS = ("gpu", "gpu_id", "device", "device_id", "id")


@dataclass(slots=True)
class AmdSmiDeviceMetric:
    """Hold one AMD-SMI device utilization observation."""

    device: str
    gpu_util_pct: float | None
    vram_used_mb: float | None


@dataclass(slots=True)
class HostMetric:
    """Hold one host CPU and RAM observation."""

    available: bool
    reason: str = ""
    cpu_util_pct: float | None = None
    ram_used_mb: float | None = None


class HostResourceSampler:
    """Sample host CPU and RAM through optional psutil support."""

    async def sample(self) -> HostMetric:
        """Return host resource values or an explicit dependency reason."""

        try:
            import psutil
        except ImportError:
            return HostMetric(False, "psutil_not_installed")
        cpu = await asyncio.to_thread(psutil.cpu_percent, None)
        memory = psutil.virtual_memory()
        return HostMetric(
            available=True,
            cpu_util_pct=float(cpu),
            ram_used_mb=float(memory.used) / (1024 * 1024),
        )


class AmdSmiSampler:
    """Sample AMD GPU utilization and VRAM using JSON with CSV fallback."""

    def __init__(self, binary: str = "amd-smi", timeout_seconds: float = 8.0) -> None:
        """Store the command path and bounded invocation timeout."""

        self.binary = binary
        self.timeout_seconds = timeout_seconds

    async def sample(
        self,
        scope: EvidenceScope,
    ) -> tuple[list[AmdSmiDeviceMetric], str]:
        """Return host GPU samples only for declared cloud-inference hosts."""

        if scope is EvidenceScope.CONTROL_PLANE_ONLY:
            return [], "disabled_for_control_plane_only"
        json_result = await asyncio.to_thread(self._run, "--json")
        if json_result is not None:
            samples = parse_amd_smi_json(json_result)
            if samples:
                return samples, ""
        csv_result = await asyncio.to_thread(self._run, "--csv")
        if csv_result is not None:
            samples = parse_amd_smi_csv(csv_result)
            if samples:
                return samples, ""
        return [], "amd_smi_unavailable_or_unparseable"

    def _run(self, output_flag: str) -> str | None:
        """Invoke one non-watching AMD-SMI monitor snapshot safely."""

        try:
            completed = subprocess.run(
                [self.binary, "monitor", output_flag],
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None
        return completed.stdout if completed.returncode == 0 else None


class TelemetryCollector:
    """Collect vLLM, host, and AMD-SMI samples during one steady window."""

    def __init__(
        self,
        *,
        run_id: str,
        strategy: str,
        stage: str,
        repeat: int,
        scope: EvidenceScope,
        endpoints: tuple[EndpointSpec, ...],
        interval_seconds: float,
        metrics_client: VllmMetricsClient | None,
        host_sampler: HostResourceSampler | None = None,
        amd_smi_sampler: AmdSmiSampler | None = None,
    ) -> None:
        """Store collector labels and injectable telemetry adapters."""

        self.run_id = run_id
        self.strategy = strategy
        self.stage = stage
        self.repeat = repeat
        self.scope = scope
        self.endpoints = endpoints
        self.interval_seconds = interval_seconds
        self.metrics_client = metrics_client
        self.host_sampler = host_sampler or HostResourceSampler()
        self.amd_smi_sampler = amd_smi_sampler or AmdSmiSampler()
        self.samples: list[TelemetrySample] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start periodic sampling in the current event loop."""

        self._task = asyncio.create_task(self._run())

    async def stop(self) -> list[TelemetrySample]:
        """Stop sampling and return every collected observation."""

        self._stop.set()
        if self._task is not None:
            await self._task
        return self.samples

    async def _run(self) -> None:
        """Sample immediately and then at the configured interval."""

        while True:
            await self._sample_once()
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue

    async def _sample_once(self) -> None:
        """Collect all telemetry sources concurrently for one timestamp."""

        host_task = asyncio.create_task(self.host_sampler.sample())
        amd_task = asyncio.create_task(self.amd_smi_sampler.sample(self.scope))
        metric_tasks = []
        if self.metrics_client is not None:
            metric_tasks = [
                asyncio.create_task(self.metrics_client.sample(endpoint))
                for endpoint in self.endpoints
            ]
        timestamp = datetime.now(UTC).isoformat()
        host = await host_task
        amd_metrics, amd_reason = await amd_task
        self.samples.append(self._host_sample(timestamp, host))
        self.samples.extend(self._amd_samples(timestamp, amd_metrics, amd_reason))
        if metric_tasks:
            for snapshot in await asyncio.gather(*metric_tasks):
                self.samples.append(self._vllm_sample(timestamp, snapshot))
        else:
            self.samples.append(self._missing_vllm_sample(timestamp))

    def _host_sample(self, timestamp: str, host: HostMetric) -> TelemetrySample:
        """Attach benchmark labels and evidence scope to a host sample."""

        return TelemetrySample(
            run_id=self.run_id,
            strategy=self.strategy,
            stage=self.stage,
            repeat=self.repeat,
            timestamp=timestamp,
            source="host",
            scope=self.scope.value,
            target="benchmark_host",
            available=host.available,
            reason=host.reason,
            cpu_util_pct=host.cpu_util_pct,
            ram_used_mb=host.ram_used_mb,
        )

    def _amd_samples(
        self,
        timestamp: str,
        metrics: list[AmdSmiDeviceMetric],
        reason: str,
    ) -> list[TelemetrySample]:
        """Attach benchmark labels to AMD-SMI values or an unavailable row."""

        if not metrics:
            return [
                TelemetrySample(
                    self.run_id,
                    self.strategy,
                    self.stage,
                    self.repeat,
                    timestamp,
                    "amd_smi",
                    self.scope.value,
                    "benchmark_host",
                    False,
                    reason,
                )
            ]
        return [
            TelemetrySample(
                self.run_id,
                self.strategy,
                self.stage,
                self.repeat,
                timestamp,
                "amd_smi",
                self.scope.value,
                metric.device,
                True,
                gpu_util_pct=metric.gpu_util_pct,
                vram_used_mb=metric.vram_used_mb,
            )
            for metric in metrics
        ]

    def _vllm_sample(self, timestamp: str, snapshot: Any) -> TelemetrySample:
        """Attach benchmark labels to one vLLM metrics snapshot."""

        return TelemetrySample(
            self.run_id,
            self.strategy,
            self.stage,
            self.repeat,
            timestamp,
            "vllm_metrics",
            "inference_server",
            snapshot.endpoint,
            snapshot.available,
            snapshot.reason,
            queue_length=snapshot.queue_length,
            running_requests=snapshot.running_requests,
            queue_time_sum_seconds=snapshot.queue_time_sum_seconds,
            queue_time_count=snapshot.queue_time_count,
            inference_time_sum_seconds=snapshot.inference_time_sum_seconds,
            inference_time_count=snapshot.inference_time_count,
        )

    def _missing_vllm_sample(self, timestamp: str) -> TelemetrySample:
        """Record that server metrics were intentionally disabled."""

        return TelemetrySample(
            self.run_id,
            self.strategy,
            self.stage,
            self.repeat,
            timestamp,
            "vllm_metrics",
            "inference_server",
            "not_configured",
            False,
            "metrics_client_disabled",
        )


def parse_amd_smi_json(text: str) -> list[AmdSmiDeviceMetric]:
    """Parse AMD-SMI JSON across flat and nested output shapes."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    results: list[AmdSmiDeviceMetric] = []
    for path, record in _candidate_records(payload):
        flattened = _flatten_mapping(record)
        gpu_util = _metric_from_mapping(flattened, _GPU_UTIL_KEYS, percent=True)
        vram = _metric_from_mapping(flattened, _VRAM_USED_KEYS, megabytes=True)
        if gpu_util is None and vram is None:
            continue
        device = _device_name(flattened, path, len(results))
        results.append(AmdSmiDeviceMetric(device, gpu_util, vram))
    return _deduplicate_devices(results)


def parse_amd_smi_csv(text: str) -> list[AmdSmiDeviceMetric]:
    """Parse AMD-SMI CSV monitor output and normalize units."""

    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    results: list[AmdSmiDeviceMetric] = []
    for index, row in enumerate(reader):
        normalized = {_normalize_key(key): value for key, value in row.items() if key is not None}
        gpu_util = _metric_from_mapping(normalized, _GPU_UTIL_KEYS, percent=True)
        vram = _metric_from_mapping(normalized, _VRAM_USED_KEYS, megabytes=True)
        if gpu_util is None and vram is None:
            continue
        results.append(AmdSmiDeviceMetric(_device_name(normalized, "", index), gpu_util, vram))
    return results


def _candidate_records(
    value: Any,
    path: str = "",
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Yield the highest nested mappings that contain GPU metrics."""

    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _candidate_records(item, f"{path}/{index}")
        return
    if not isinstance(value, dict):
        return
    flattened = _flatten_mapping(value)
    if _has_metric_key(flattened):
        yield path, value
        return
    for key, item in value.items():
        yield from _candidate_records(item, f"{path}/{key}")


def _flatten_mapping(
    value: Mapping[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    """Flatten nested AMD-SMI mappings into normalized underscore keys."""

    flattened: dict[str, Any] = {}
    for key, item in value.items():
        normalized = _normalize_key(str(key))
        full_key = f"{prefix}_{normalized}".strip("_")
        if isinstance(item, dict):
            flattened.update(_flatten_mapping(item, full_key))
        else:
            flattened[full_key] = item
    return flattened


def _metric_from_mapping(
    values: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    percent: bool = False,
    megabytes: bool = False,
) -> float | None:
    """Find a normalized metric key and parse its numeric value."""

    for key, value in values.items():
        if any(key == candidate or key.endswith(f"_{candidate}") for candidate in keys):
            return _parse_metric_number(value, percent=percent, megabytes=megabytes)
    return None


def _parse_metric_number(
    value: Any,
    *,
    percent: bool,
    megabytes: bool,
) -> float | None:
    """Parse numeric AMD-SMI values while preserving N/A as unavailable."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        parsed = float(value)
    elif isinstance(value, str) and value.strip().upper() not in {"N/A", "NA", ""}:
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value.replace(",", ""))
        if match is None:
            return None
        parsed = float(match.group())
        unit = value.upper()
        if megabytes and "GB" in unit:
            parsed *= 1024
        elif megabytes and "KB" in unit:
            parsed /= 1024
        elif megabytes and "BYTE" in unit and "MB" not in unit:
            parsed /= 1024 * 1024
    else:
        return None
    if not math.isfinite(parsed):
        return None
    if percent and not 0 <= parsed <= 100:
        return None
    return parsed


def _device_name(values: Mapping[str, Any], path: str, index: int) -> str:
    """Return an AMD-SMI device identifier without inventing hardware claims."""

    for key, value in values.items():
        if any(
            key == candidate or key.endswith(f"_{candidate}") for candidate in _DEVICE_KEYS
        ) and isinstance(value, str | int):
            return f"gpu_{value}"
    path_name = path.strip("/").replace("/", "_")
    return path_name or f"gpu_{index}"


def _has_metric_key(values: Mapping[str, Any]) -> bool:
    """Return whether a flattened mapping contains utilization or VRAM."""

    candidates = (*_GPU_UTIL_KEYS, *_VRAM_USED_KEYS)
    return any(
        key == candidate or key.endswith(f"_{candidate}")
        for key in values
        for candidate in candidates
    )


def _normalize_key(value: str) -> str:
    """Normalize telemetry headers for schema-tolerant matching."""

    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _deduplicate_devices(values: list[AmdSmiDeviceMetric]) -> list[AmdSmiDeviceMetric]:
    """Remove duplicate nested records while preserving the first sample."""

    seen: set[tuple[str, float | None, float | None]] = set()
    result: list[AmdSmiDeviceMetric] = []
    for item in values:
        key = (item.device, item.gpu_util_pct, item.vram_used_mb)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
