from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class EdgeDevice:
    """Tenant/store scope and volatile signing secret for one provisioned edge device."""

    device_id: str
    tenant_id: str
    store_id: str
    hmac_secret: bytes
    active: bool = True


class InMemoryEdgeDeviceRegistry:
    """Process-local registry used until durable device provisioning is selected."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._devices: dict[str, EdgeDevice] = {}
        self._batches: set[tuple[str, str]] = set()

    def register(self, device: EdgeDevice) -> None:
        """Register or replace one device scope without exposing its secret."""
        if not device.device_id.strip() or not device.hmac_secret:
            raise ValueError("device_id and hmac_secret are required")
        with self._lock:
            self._devices[device.device_id] = device

    def provision(self, device: EdgeDevice) -> dict[str, str]:
        """Provision a device and return non-secret identity metadata."""
        self.register(device)
        return {
            "device_id": device.device_id, "tenant_id": device.tenant_id,
            "store_id": device.store_id,
        }

    def list_devices(self, tenant_id: str, store_id: str | None = None) -> list[dict[str, object]]:
        """List device health metadata without returning signing secrets."""
        with self._lock:
            rows = [
                {
                    "device_id": item.device_id, "tenant_id": item.tenant_id,
                    "store_id": item.store_id, "active": item.active,
                }
                for item in self._devices.values()
                if item.tenant_id == tenant_id and (store_id is None or item.store_id == store_id)
            ]
        return sorted(rows, key=lambda row: str(row["device_id"]))

    def get_active(self, device_id: str) -> EdgeDevice | None:
        """Return an active device record or no record for failed-closed auth."""
        with self._lock:
            device = self._devices.get(device_id)
        return device if device and device.active else None

    def revoke(self, device_id: str) -> bool:
        """Disable a device without deleting its audit identity."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            self._devices[device_id] = EdgeDevice(
                device_id=device.device_id,
                tenant_id=device.tenant_id,
                store_id=device.store_id,
                hmac_secret=device.hmac_secret,
                active=False,
            )
            return True

    def record_batch(self, tenant_id: str, batch_id: str) -> bool:
        """Record one tenant/batch pair and reject an exact replay."""
        key = (tenant_id, batch_id)
        with self._lock:
            if key in self._batches:
                return False
            self._batches.add(key)
            return True

    def clear(self) -> None:
        """Clear disposable registry state between tests."""
        with self._lock:
            self._devices.clear()
            self._batches.clear()
