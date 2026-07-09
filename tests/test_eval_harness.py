from __future__ import annotations

from shelfwise_eval import run_backend_eval


def test_backend_eval_harness_covers_demo_critical_beats() -> None:
    report = run_backend_eval(token_ceiling=24_000)
    checks = {check.name: check for check in report.checks}

    assert report.passed is True
    assert checks["golden_pending_hitl"].passed is True
    assert checks["trace_chain"].passed is True
    assert checks["pending_to_approved"].passed is True
    assert checks["critic_rejection_downgrades"].passed is True
    assert checks["tool_catalog_read_only"].passed is True
    assert checks["product_attention_bounded"].passed is True
    assert checks["product_search_attention_ranked"].passed is True
    assert checks["fefo_split_batch_math"].passed is True
    assert checks["delivery_reconcile_exception_math"].passed is True
    assert checks["supplier_cover_action_math"].passed is True
    assert checks["outcome_summary_learning_math"].passed is True
    assert report.to_dict()["passed_count"] == report.total_count
