"""A named news-tone rule strategy for an operator-run forward test.

Discovery admits a news rule that clears the fixed gate as ``rule_<id>`` like
any other candidate (``strategy_discovery.NEWS_FAMILIES``).  This class is the
same ``RuleStrategy`` machinery under a stable slug, ``news_tone``, so an
operator can forward-test one news rule on paper before the history is long
enough for the gate, with its own attribution, sizing and meta-label record.
It is registered but never seeded: a ``Strategy`` row of this type is created
by hand with ``parameters["rule"]`` (default: positive tone over at least two
stories in an uptrend, out above the 5-day average or after 5 sessions) and
``symbol_scope``.  Paper only.
"""
from __future__ import annotations

from typing import Optional

from app.services.strategies.base import ExitPolicy
from app.services.strategies.rules import RuleStrategy

DEFAULT_RULE = {
    "entry": [{"f": "dist_sma_200", "op": ">", "v": 0}, {"f": "news_tone_1", "op": ">=", "v": 0.5},
              {"f": "news_articles_1", "op": ">=", "v": 2}],
    "exit": [{"f": "dist_sma_5", "op": ">", "v": 0}],
}


class NewsToneStrategy(RuleStrategy):
    """``RuleStrategy`` with a news-tone default rule and the news family's exit plan."""

    name = "News Tone Rule"
    slug = "news_tone"
    default_exit_policy = ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0, max_holding_sessions=5,
                                     indicator_exit=True)

    def __init__(self, parameters: Optional[dict] = None, slug: str = "news_tone") -> None:
        parameters = dict(parameters or {})
        parameters.setdefault("rule", DEFAULT_RULE)
        super().__init__(parameters, slug=slug)
