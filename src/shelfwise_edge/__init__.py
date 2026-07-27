"""Privacy-preserving edge intake for derived digital-twin observations."""

from .contracts import EdgeObservationBatch
from .registry import (
    EdgeDevice,
    EdgeDeviceRegistrationError,
    InMemoryEdgeDeviceRegistry,
    PostgresEdgeDeviceRegistry,
    create_edge_device_registry,
)
from .signing import MAX_EDGE_BODY_BYTES, verify_signed_body

edge_device_registry = create_edge_device_registry()

__all__ = [
    "MAX_EDGE_BODY_BYTES",
    "EdgeDevice",
    "EdgeDeviceRegistrationError",
    "EdgeObservationBatch",
    "InMemoryEdgeDeviceRegistry",
    "PostgresEdgeDeviceRegistry",
    "create_edge_device_registry",
    "edge_device_registry",
    "verify_signed_body",
]
