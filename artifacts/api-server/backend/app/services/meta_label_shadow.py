"""Runtime wiring of the meta-label filter into the paper decision path.

Modes (``settings.paper_meta_label_mode``):

* ``off``: nothing is computed or recorded.
* ``shadow`` (default): every actionable BUY gets the calibrated probability,
  the threshold and the decision the filter *would* make recorded in the
  signal's persisted features, the cycle result and the log.  Orders are
  unchanged.  A missing or invalid model artifact degrades to ``off`` (fail
  open), so the hosted app keeps working without it.
* ``enforce``: BUYs the filter does not mark ``take`` are skipped.  When the
  artifact is unavailable, or a signal cannot be scored, the BUY is skipped
  (fail closed).  Sells and protective exits are never filtered.

Nothing here can submit, reserve or dispatch an order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.core.config import settings

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_RELATIVE_PATH = Path("stock_training_artifacts") / "meta_label" / "meta_label_model.joblib"


def model_path() -> Path:
    configured = (settings.paper_meta_label_model_path or "").strip()
    if not configured:
        return BACKEND_ROOT / DEFAULT_MODEL_RELATIVE_PATH
    path = Path(configured)
    return path if path.is_absolute() else BACKEND_ROOT / path


@dataclass
class MetaLabelRuntime:
    configured_mode: str
    mode: str
    path: str
    bundle: dict | None = None
    reason: str | None = None
    scored: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.mode in {"shadow", "enforce"}

    def summary(self) -> dict:
        bundle = self.bundle or {}
        decisions = [row.get("decision") for row in self.scored]
        return {
            "configured_mode": self.configured_mode,
            "mode": self.mode,
            "model_path": self.path,
            "model_available": self.bundle is not None,
            "model_sha256": bundle.get("sha256"),
            "threshold": bundle.get("threshold"),
            "reason": self.reason,
            "buy_signals_evaluated": len(self.scored),
            "would_take": decisions.count("take"),
            "would_skip": sum(decision != "take" for decision in decisions),
        }

    def evaluate_symbol(self, symbol: str, prices: Any, spy_prices: Any,
                        buys: Iterable[tuple[str, Any]]) -> dict[str, dict]:
        """Score each ``(strategy_type, generated_signal)`` BUY for one symbol.

        Never raises: any failure becomes a recorded ``status`` with decision
        ``skip`` in enforce mode and ``None`` (no opinion) in shadow mode.
        """
        buys = list(buys)
        if not self.active or not buys:
            return {}
        base = {"mode": self.mode, "threshold": (self.bundle or {}).get("threshold"),
                "model_sha256": (self.bundle or {}).get("sha256")}
        out: dict[str, dict] = {}
        if self.bundle is None:
            for strategy_type, _generated in buys:
                out[strategy_type] = base | {"status": "model_unavailable", "probability": None,
                                             "decision": "skip", "reason": self.reason}
        else:
            from app.services import meta_label

            try:
                frame = prices if isinstance(prices, pd.DataFrame) else None
                spy = spy_prices if isinstance(spy_prices, pd.DataFrame) else None
                latest = meta_label.latest_market_features(frame, spy) if frame is not None else None
            except Exception as exc:  # feature failure must not break the cycle
                latest, error = None, f"{exc.__class__.__name__}: {exc}"
            else:
                error = None
            for strategy_type, generated in buys:
                try:
                    if error is not None:
                        raise RuntimeError(error)
                    scored = meta_label.score_buy_signal(
                        self.bundle, symbol, strategy_type, float(generated.confidence),
                        float(generated.probability_up), frame, spy, latest=latest,
                    )
                except Exception as exc:
                    scored = {"status": "error", "probability": None, "decision": None,
                              "reason": f"{exc.__class__.__name__}: {exc}"}
                if scored.get("decision") is None and self.mode == "enforce":
                    scored = scored | {"decision": "skip"}
                out[strategy_type] = base | scored
        for strategy_type, record in out.items():
            self.scored.append(record)
            logger.info(
                "meta-label %s: symbol=%s strategy=%s status=%s probability=%s threshold=%s decision=%s",
                self.mode, symbol, strategy_type, record.get("status"), record.get("probability"),
                record.get("threshold"), record.get("decision"),
            )
        return out

    def allows_buy(self, record: dict | None) -> tuple[bool, str | None]:
        """Only ``enforce`` can veto; shadow and off always allow."""
        if self.mode != "enforce":
            return True, None
        if record is None:
            return False, "meta-label enforce: BUY was not scored (fail closed)"
        if record.get("decision") == "take":
            return True, None
        return False, f"meta-label enforce: {record.get('reason') or record.get('status')}"


def resolve_runtime() -> MetaLabelRuntime:
    configured = str(settings.paper_meta_label_mode or "off").strip().lower()
    path = model_path()
    if configured not in {"shadow", "enforce"}:
        return MetaLabelRuntime(configured_mode=configured, mode="off", path=str(path))
    from app.services import meta_label

    try:
        bundle = meta_label.load_model(path)
    except Exception as exc:
        reason = f"meta-label model unavailable at {path}: {exc.__class__.__name__}: {exc}"
        if configured == "enforce":
            logger.warning("%s; enforce mode skips every BUY (fail closed)", reason)
            return MetaLabelRuntime(configured_mode=configured, mode="enforce", path=str(path), reason=reason)
        logger.info("%s; shadow mode is off for this cycle", reason)
        return MetaLabelRuntime(configured_mode=configured, mode="off", path=str(path), reason=reason)
    return MetaLabelRuntime(configured_mode=configured, mode=configured, path=str(path), bundle=bundle)
