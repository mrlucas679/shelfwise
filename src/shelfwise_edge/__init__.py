"""Privacy-preserving edge intake for derived digital-twin observations."""

from .contracts import EdgeObservationBatch
from .registry import EdgeDevice, InMemoryEdgeDeviceRegistry
from .signing import MAX_EDGE_BODY_BYTES, verify_signed_body

edge_device_registry = InMemoryEdgeDeviceRegistry()

__all__ = [
    "MAX_EDGE_BODY_BYTES",
    "EdgeDevice",
    "EdgeObservationBatch",
    "InMemoryEdgeDeviceRegistry",
    "edge_device_registry",
    "verify_signed_body",
]
