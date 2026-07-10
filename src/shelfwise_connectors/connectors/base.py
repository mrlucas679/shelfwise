from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..canonical import SourceSystem
from ..provenance import InboundRecord


class SourceConnector(ABC):
    source_system: SourceSystem

    @abstractmethod
    def pull(self) -> AsyncIterator[InboundRecord]:
        """Yield provenance-wrapped records from a read-only source."""
        raise NotImplementedError
