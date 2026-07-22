from __future__ import annotations

from shelfwise_connectors.canonical import SourceSystem
from shelfwise_connectors.connectors.base import SourceConnector
from shelfwise_connectors.connectors.systems.dynamics import (
    DynamicsBusinessCentralInventoryConnector,
)
from shelfwise_connectors.connectors.systems.odoo import OdooProductConnector
from shelfwise_connectors.connectors.systems.registry import POLL_MAPPERS, WEBHOOK_MAPPERS
from shelfwise_connectors.connectors.systems.sap import SapS4InventoryConnector
from shelfwise_connectors.connectors.systems.syspro import SysproInventoryConnector

# The connector layer's contract is a real, checkable type (`SourceConnector`, an ABC
# with an abstract `pull()`), not a convention every connector author has to remember to
# follow - Python already refuses to instantiate a subclass that forgot `pull()`. What
# is NOT mechanically checked anywhere is the other half of the contract this layer
# depends on for correctness: that every `SourceSystem` the canonical model knows about
# actually has exactly one connector wired into dispatch. `csv` is intentionally the one
# exception - it has its own dedicated ingest path in shelfwise_data, not this registry.
_SOURCE_SYSTEMS_WITH_A_DEDICATED_PATH_OUTSIDE_THIS_REGISTRY = {SourceSystem.CSV}

_POLL_CONNECTOR_CLASSES = {
    SourceSystem.DYNAMICS: DynamicsBusinessCentralInventoryConnector,
    SourceSystem.ODOO: OdooProductConnector,
    SourceSystem.SAP: SapS4InventoryConnector,
    SourceSystem.SYSPRO: SysproInventoryConnector,
}


def test_every_poll_connector_class_is_a_real_source_connector_subclass() -> None:
    """The base contract is a checkable ABC, not just a name - prove it, don't assert it.

    `issubclass` here is the mechanical version of "is this a type the compiler/CI can
    hold connector authors to" - if a future poll connector were added as a bare class
    that merely duck-types `pull()`, this fails instead of silently working until the
    dispatcher calls a method the object doesn't actually have.
    """
    for system, connector_cls in _POLL_CONNECTOR_CLASSES.items():
        assert issubclass(connector_cls, SourceConnector), (
            f"{connector_cls.__name__} (registered for {system.value}) does not subclass "
            "SourceConnector - the pull() contract is unenforced for this connector"
        )
        assert connector_cls.source_system == system, (
            f"{connector_cls.__name__}.source_system does not match its registry key"
        )


def test_every_dispatchable_source_system_has_exactly_one_registered_mapper() -> None:
    """Catch a `SourceSystem` added to the canonical model but never wired into dispatch.

    `map_for` in registry.py raises ValueError at call time for an unregistered system -
    correct, but that only fires once real traffic hits it. This makes the same gap fail
    at collection time instead, and also catches the opposite mistake (a system wired
    into both POLL_MAPPERS and WEBHOOK_MAPPERS, which would make dispatch ambiguous).
    """
    all_systems = set(SourceSystem)
    dispatchable = all_systems - _SOURCE_SYSTEMS_WITH_A_DEDICATED_PATH_OUTSIDE_THIS_REGISTRY
    poll_systems = set(POLL_MAPPERS)
    webhook_systems = set(WEBHOOK_MAPPERS)

    unwired = dispatchable - poll_systems - webhook_systems
    assert not unwired, (
        f"{[s.value for s in unwired]} are real SourceSystem values with no poll or "
        "webhook mapper registered in registry.py - ingest for these would fail at "
        "runtime with 'no connector mapper registered', not at review time"
    )

    ambiguous = poll_systems & webhook_systems
    assert not ambiguous, (
        f"{[s.value for s in ambiguous]} are registered as BOTH a poll and a webhook "
        "source - map_for() would silently prefer the webhook mapper every time"
    )

    stale = (poll_systems | webhook_systems) - all_systems
    assert not stale, (
        f"{[s.value for s in stale]} are registered in registry.py but no longer exist "
        "on the SourceSystem enum - dead dispatch entries"
    )
