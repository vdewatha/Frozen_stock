from app.services.scalp_research import _metrics, create_scalp_research_run


def test_scalp_metrics_are_separate_and_cost_adjusted():
    values = [100 + index * 0.05 for index in range(80)]
    result = _metrics(values)
    assert set(result) == {"ema_momentum", "intraday_mean_reversion"}
    assert result["ema_momentum"]["trades"] > 0
    assert result["ema_momentum"]["average_trade"] < 0.01


def test_scalp_run_rejects_unapproved_symbols():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.models import ScalpResearchRun

    engine = create_engine("sqlite://")
    ScalpResearchRun.__table__.create(engine)
    with Session(engine) as db:
        try:
            create_scalp_research_run(db, symbols=["TSLA"], actor="test")
        except ValueError as exc:
            assert "supports only" in str(exc)
        else:
            raise AssertionError("unapproved symbol was accepted")
