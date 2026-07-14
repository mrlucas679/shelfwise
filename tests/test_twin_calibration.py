from shelfwise_twin import InMemoryCalibrationRegistry
from shelfwise_twin.service import _calibration_complete


def test_calibration_score_is_bounded_and_scoped() -> None:
    registry = InMemoryCalibrationRegistry()
    record = registry.record(
        tenant_id="t", store_id="s", device_id="device-01", property_name="temperature.c",
        reference_value=4, observed_value=5, tolerance=2,
    )
    assert record.score == 0.5
    assert registry.score("t", "s") == 0.5
    assert registry.score("other", "s") == 0.0


def test_calibration_complete_requires_every_expected_device_calibrated() -> None:
    registry = InMemoryCalibrationRegistry()
    registry.record(
        tenant_id="t", store_id="s", device_id="device-01", property_name="temperature.c",
        reference_value=4, observed_value=4, tolerance=2,
    )

    # No provisioned devices at all must not vacuously pass as "complete".
    assert _calibration_complete(frozenset(), registry.list("t", "s")) is False
    assert _calibration_complete(None, registry.list("t", "s")) is False

    # One of two expected devices calibrated is not complete.
    assert (
        _calibration_complete(frozenset({"device-01", "device-02"}), registry.list("t", "s"))
        is False
    )

    # Every expected device calibrated is complete.
    assert _calibration_complete(frozenset({"device-01"}), registry.list("t", "s")) is True
