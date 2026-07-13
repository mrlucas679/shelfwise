from __future__ import annotations

from itertools import islice

from shelfwise_worldgen.fleet import iter_fleet_batch_states, score_fleet_expiry


def test_fleet_expiry_scoring_is_streaming_bounded_and_ranked() -> None:
    rows = islice(iter_fleet_batch_states(20260712, locations=20), 12_500)
    summary = score_fleet_expiry(rows, chunk_size=1_000, top_limit=25)

    assert summary.rows_processed == 12_500
    assert summary.chunks_processed == 13
    assert summary.candidates_crossing_threshold >= len(summary.top_candidates)
    assert 0 < len(summary.top_candidates) <= 25
    assert list(summary.top_candidates) == sorted(
        summary.top_candidates,
        key=lambda candidate: (candidate.risk, candidate.zar_at_risk.minor_units),
        reverse=True,
    )


def test_fleet_expiry_scoring_rejects_invalid_runtime_limits() -> None:
    try:
        score_fleet_expiry([], chunk_size=0)
    except ValueError as exc:
        assert str(exc) == "chunk_size must be positive"
    else:
        raise AssertionError("expected chunk_size validation")
