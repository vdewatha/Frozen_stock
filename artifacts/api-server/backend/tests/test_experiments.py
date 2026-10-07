from app.services.experiments import _decision


def _result(*, score, rejected=False, trades=20, drawdown=0.10):
    return {
        "score": score,
        "rejected": rejected,
        "number_of_trades": trades,
        "max_drawdown": drawdown,
    }


def test_in_sample_improvement_requires_out_of_sample_validation():
    decision, reason = _decision(_result(score=0.20), _result(score=0.24))

    assert decision == "needs_more_data"
    assert "out-of-sample" in reason


def test_promotion_requires_validation_improvement():
    decision, reason = _decision(
        _result(score=0.20),
        _result(score=0.24),
        validation_old=_result(score=0.10),
        validation_new=_result(score=0.14),
    )

    assert decision == "promoted"
    assert "Out-of-sample" in reason


def test_failed_validation_rejects_in_sample_improvement():
    decision, reason = _decision(
        _result(score=0.20),
        _result(score=0.24),
        validation_old=_result(score=0.10),
        validation_new=_result(score=0.20, rejected=True),
    )

    assert decision == "rejected"
    assert "validation" in reason
