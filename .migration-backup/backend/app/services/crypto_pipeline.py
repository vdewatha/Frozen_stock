"""Transaction-isolated collection and shadow orchestration. Never sends orders."""
from datetime import datetime, timezone
import logging
from sqlalchemy import select
from app.models.shadow import ShadowModelBinding
from app.services.crypto_collection import collect_kraken
from app.services.shadow_pipeline import run_shadow, score_shadow

logger = logging.getLogger(__name__)


def run_crypto_cycle(session_factory, *, client=None, as_of=None):
    """Commit collection before inference; each stage owns a fresh transaction.

    ``as_of`` is an internal deterministic-test seam, not a public input. All
    decisions remain research-only regardless of the provided clock.
    """
    clock = as_of if as_of is not None else datetime.now(timezone.utc)
    result = {"status": "blocked", "collection_run_id": None, "inserted_count": 0,
              "observed_count": 0, "blocked_bindings": [], "scored_count": 0}
    with session_factory.begin() as db:
        collection = collect_kraken(db, client=client, as_of=clock)
        result.update(collection_run_id=collection.id, inserted_count=collection.inserted_count)
        collection_ok = collection.status == "success"
        result["collection_error"] = collection.error_code
    if not collection_ok:
        return result
    with session_factory() as db:
        binding_ids = list(db.scalars(select(ShadowModelBinding.id).order_by(ShadowModelBinding.id)))
    for binding_id in binding_ids:
        try:
            with session_factory.begin() as db:
                decision = run_shadow(db, binding_id, as_of=clock)
                if decision is None:
                    result["blocked_bindings"].append(binding_id)
                else:
                    result["observed_count"] += 1
        except Exception:
            # Never log exception strings, connection URLs or serialized models.
            logger.error("crypto_shadow_stage_failed binding_id=%s", binding_id)
            result["blocked_bindings"].append(binding_id)
    with session_factory.begin() as db:
        result["scored_count"] = score_shadow(db, as_of=clock)
    result["status"] = "partial" if result["blocked_bindings"] else "success"
    return result
