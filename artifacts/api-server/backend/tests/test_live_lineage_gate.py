from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models import StockPaperBindingState, StockPaperModelBinding, StockModelRegistry, StockDatasetSnapshot
from app.services.live_safety import _lineage_gate
from app.services.stock_training_jobs import StockTrainingError


def registry(metadata=None, lifecycle="champion"):
    rows = {
        StockPaperBindingState: SimpleNamespace(active_binding_id=1),
        StockPaperModelBinding: SimpleNamespace(id=1, model_run_id="model", snapshot_id="snapshot", binding_sha256="binding"),
        StockModelRegistry: SimpleNamespace(run_id="model", snapshot_id="snapshot", lifecycle_state=lifecycle,
                                           manifest_sha256="manifest", training_metadata=metadata),
        StockDatasetSnapshot: SimpleNamespace(snapshot_id="snapshot", dataset_sha256="dataset"),
    }
    return SimpleNamespace(get=lambda kind, key: rows.get(kind), scalar=lambda query: rows.get("current_lifecycle")), rows


@pytest.mark.parametrize("metadata", [None, {}, {"live_eligible": True},
    {"eligible_for_trading": True}, {"live_eligible": "true", "eligible_for_trading": True},
    {"live_eligible": True, "eligible_for_trading": 1},
    {"live_eligible": False, "eligible_for_trading": True}])
def test_unknown_or_nonboolean_eligibility_cannot_pass_live_lineage(metadata):
    db, _ = registry(metadata)
    assert _lineage_gate(db)["status"] == "fail"


def test_legacy_challenger_without_metadata_cannot_pass():
    db, _ = registry({}, "challenger")
    assert _lineage_gate(db)["status"] == "fail"


@pytest.mark.parametrize("state", ["demoted", "retired", "challenger"])
def test_current_lifecycle_revokes_stale_registry_eligibility(state):
    db, rows = registry({"live_eligible": True, "eligible_for_trading": True})
    rows["current_lifecycle"] = state
    result = _lineage_gate(db)
    assert result["status"] == "fail"
    assert result["evidence"]["lifecycle_state"] == state


def test_cross_snapshot_binding_is_rejected_before_loading_artifacts():
    db, rows = registry({"live_eligible": True, "eligible_for_trading": True})
    rows[StockModelRegistry].snapshot_id = "different"
    with patch("app.services.stock_training_jobs._dataset_from_record") as load:
        assert _lineage_gate(db)["status"] == "fail"
        load.assert_not_called()


@pytest.mark.parametrize("failure_at", ["dataset", "model"])
def test_missing_or_tampered_artifacts_do_not_pass(failure_at):
    db, _ = registry({"live_eligible": True, "eligible_for_trading": True})
    failure = StockTrainingError("private-path-secret-must-not-leak")
    with patch("app.services.stock_training_jobs._dataset_from_record", side_effect=failure if failure_at == "dataset" else None), \
         patch("app.services.stock_training_jobs.validate_registered_stock_model", side_effect=failure if failure_at == "model" else None):
        result = _lineage_gate(db)
    assert result["status"] == "fail"
    assert "private-path" not in str(result)


def test_explicit_eligibility_still_requires_both_artifact_verifiers():
    db, rows = registry({"live_eligible": True, "eligible_for_trading": True}, "challenger")
    rows["current_lifecycle"] = "champion"
    dataset = object()
    with patch("app.services.stock_training_jobs._dataset_from_record", return_value=dataset) as load, \
         patch("app.services.stock_training_jobs.validate_registered_stock_model") as verify:
        result = _lineage_gate(db)
    load.assert_called_once_with(rows[StockDatasetSnapshot])
    verify.assert_called_once_with(rows[StockModelRegistry], dataset)
    assert result["status"] == "pass"
    assert result["evidence"]["artifact_integrity_verified"] is True
