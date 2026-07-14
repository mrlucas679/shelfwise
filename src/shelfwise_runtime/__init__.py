"""Runtime paths and provenance shared by application execution environments."""

from .paths import durable_dir, durable_path, persistence_root
from .provenance import (
    TRAINING_DOMAINS,
    DataDomain,
    DataDomainBoundaryError,
    normalize_domain,
    require_training_domain,
)

__all__ = [
    "TRAINING_DOMAINS",
    "DataDomain",
    "DataDomainBoundaryError",
    "durable_dir",
    "durable_path",
    "normalize_domain",
    "persistence_root",
    "require_training_domain",
]
