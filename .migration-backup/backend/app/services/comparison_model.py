"""Strict inert v3 artifact reader for isolated comparison, never a broker."""
import hashlib
import json
from pathlib import Path
import stat
import numpy as np
import pandas as pd
from app.services.feature_pipeline import feature_config_id, generate_features
from app.services.research_training_v2 import FEATURES, _json
from app.services.research_training_v3 import predict_portable
from app.services.crypto_collection import _check
from app.services.instruments import utc_timestamp
from datetime import datetime, timedelta

IDENTITY = ("format", "dataset_sha256", "source_claim", "instrument", "timeframe_minutes",
            "feature_config_id", "horizon_bars", "fee_rate_per_side", "slippage_rate_per_side",
            "seed", "versions", "code_sha256")
FILES = {"dataset.csv", "calibrated_model.json", "final_test_predictions.csv"}


def _read(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("Artifact must be bounded regular file")
    # O_NOFOLLOW closes the symlink substitution window on supported platforms.
    import os
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit: raise ValueError("Artifact too large")
    return data


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def _number(value, low=-1e12, high=1e12):
    if type(value) not in (int, float) or not np.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid model number")
    return value


def _vector(value, size, low=-1e12, high=1e12):
    if not isinstance(value, list) or len(value) != size: raise ValueError("Invalid model vector")
    return [_number(v, low, high) for v in value]


def validate_numeric(spec):
    if spec.get("version") != 3 or spec.get("eligible_for_trading") is not False or spec.get("feature_names") != FEATURES:
        raise ValueError("Unexpected portable specification")
    if spec.get("feature_config_id") != feature_config_id(version="2") or spec.get("payoff_estimate_source") != "calibration_only":
        raise ValueError("Unknown feature/payoff identity")
    _number(spec["mean_win"], 0, 1000)
    _number(spec["mean_loss"], 0, 1)
    calibration = spec["calibration"]
    if calibration.get("method") != "sigmoid_logit" or calibration.get("probability_clip") != [1e-6, 1 - 1e-6]:
        raise ValueError("Unexpected calibration policy")
    _number(calibration["coef"])
    _number(calibration["intercept"])
    base = spec["base"]
    if base.get("kind") == "logistic_regression":
        for key in ("mean", "coef"): _vector(base[key], len(FEATURES))
        _vector(base["scale"], len(FEATURES), 1e-15)
        _number(base["intercept"])
    elif base.get("kind") == "random_forest":
        trees = base["trees"]
        if not isinstance(trees, list) or not 1 <= len(trees) <= 120: raise ValueError("Invalid forest")
        total = 0
        for tree in trees:
            count = len(tree["left"])
            total += count
            if not 1 <= count <= 20000 or total > 250000: raise ValueError("Forest node budget exceeded")
            for key in ("right", "feature", "threshold", "probability"):
                if not isinstance(tree[key], list) or len(tree[key]) != count: raise ValueError("Inconsistent tree arrays")
            _vector(tree["threshold"], count)
            _vector(tree["probability"], count, 0, 1)
            parents = [0] * count
            for node, (left, right, feature) in enumerate(zip(tree["left"], tree["right"], tree["feature"])):
                if any(type(v) is not int for v in (left, right, feature)): raise ValueError("Invalid tree index")
                if left == right == -1:
                    if feature != -2: raise ValueError("Invalid leaf")
                else:
                    if not node < left < count or not node < right < count or left == right or not 0 <= feature < len(FEATURES):
                        raise ValueError("Nonforward or invalid tree edge")
                    parents[left] += 1
                    parents[right] += 1
            if parents[0] != 0 or any(n != 1 for n in parents[1:]): raise ValueError("Disconnected or shared tree nodes")
    else:
        raise ValueError("Unknown numeric model kind")


def load_comparison_model(directory, *, fee_rate, slippage_rate, source_claim, manifest_sha256, expected_horizon=24, minimum_history=8760):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir(): raise ValueError("Model directory required")
    if any(parent.is_symlink() for parent in directory.absolute().parents): raise ValueError("Symlink model ancestor")
    manifest_bytes = _read(directory / "manifest.json", 2 * 1024 * 1024)
    if not isinstance(manifest_sha256, str) or hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        raise ValueError("Pinned manifest hash mismatch")
    manifest = _decode(manifest_bytes)
    identity = {key: manifest[key] for key in IDENTITY}
    if "selection_policy" in manifest:
        if manifest["selection_policy"] != "fixed_random_forest_v1" or manifest.get("selected_model") != "random_forest":
            raise ValueError("Unknown or inconsistent candidate selection policy")
        identity["selection_policy"] = manifest["selection_policy"]
    if "gap_policy" in manifest:
        if manifest["gap_policy"] != "segment": raise ValueError("Unknown candidate gap policy")
        identity.update({key: manifest[key] for key in ("gap_policy", "missing_hours", "missing_hours_sha256", "calendar_hours")})
    elif any(key in manifest for key in ("missing_hours", "missing_hours_sha256", "calendar_hours")):
        raise ValueError("Undeclared gap metadata")
    run_id = hashlib.sha256(_json(identity)).hexdigest()
    if (run_id != directory.name or manifest.get("run_id") != run_id or manifest.get("format") != "calibrated-research-v3"
            or manifest.get("status") != "experimental" or manifest.get("eligible_for_trading") is not False
            or manifest.get("instrument") != "crypto_spot:KRAKEN:BTC:USD" or manifest.get("timeframe_minutes") != 60
            or manifest.get("feature_config_id") != feature_config_id(version="2")
            or manifest.get("source_claim") != source_claim or not source_claim):
        raise ValueError("Candidate identity/provenance claim mismatch")
    if float(fee_rate) != manifest["fee_rate_per_side"] or float(slippage_rate) != manifest["slippage_rate_per_side"]:
        raise ValueError("Model and comparison cost assumptions differ")
    _number(manifest["fee_rate_per_side"], 0, .05)
    _number(manifest["slippage_rate_per_side"], 0, .05)
    if type(expected_horizon) is not int or not 1 <= expected_horizon <= 168 or type(manifest["horizon_bars"]) is not int or manifest["horizon_bars"] != expected_horizon:
        raise ValueError("Invalid candidate horizon")
    if set(manifest["files"]) != FILES: raise ValueError("Unexpected artifact inventory")
    payloads = {}
    for name in FILES:
        payloads[name] = _read(directory / name, 64 * 1024 * 1024)
        if hashlib.sha256(payloads[name]).hexdigest() != manifest["files"][name]: raise ValueError("Artifact hash mismatch")
    if hashlib.sha256(payloads["dataset.csv"]).hexdigest() != manifest["dataset_sha256"]: raise ValueError("Dataset identity mismatch")
    import io
    dataset_dates = pd.to_datetime(pd.read_csv(io.BytesIO(payloads["dataset.csv"]), usecols=["date"])["date"], utc=True, errors="raise")
    if dataset_dates.empty or dataset_dates.isna().any() or not dataset_dates.is_monotonic_increasing or dataset_dates.duplicated().any() or any(d.minute or d.second or d.microsecond or d.nanosecond for d in dataset_dates):
        raise ValueError("Invalid candidate dataset timestamps")
    if type(minimum_history) is not int or minimum_history < 1: raise ValueError("Invalid minimum history")
    if manifest.get("gap_policy") == "segment":
        span = int((dataset_dates.iloc[-1] - dataset_dates.iloc[0]) / pd.Timedelta(hours=1)) + 1
        if span - len(dataset_dates) > 24 or span < minimum_history: raise ValueError("Segment history coverage outside bounded policy")
        missing = [value.isoformat() for value in pd.date_range(dataset_dates.iloc[0], dataset_dates.iloc[-1], freq="h").difference(pd.DatetimeIndex(dataset_dates))]
        if manifest["missing_hours"] != missing or manifest["missing_hours_sha256"] != hashlib.sha256(_json(missing)).hexdigest() or type(manifest["calendar_hours"]) is not int or manifest["calendar_hours"] != span:
            raise ValueError("Declared missing-hour identity differs from dataset")
    elif len(dataset_dates) < minimum_history or not (dataset_dates.diff().dropna() == pd.Timedelta(hours=1)).all():
        raise ValueError("Insufficient or noncontiguous pinned research history")
    spec = _decode(payloads["calibrated_model.json"])
    validate_numeric(spec)
    if spec["base"]["kind"] != manifest.get("selected_model"):
        raise ValueError("Selected model and numeric export disagree")
    if spec.get("run_id") != run_id or spec["training_cutoff"] != manifest["train_label_end"] or spec["calibration_cutoff"] != manifest["calibration_label_end"]:
        raise ValueError("Portable model cutoff identity mismatch")
    dates = [utc_timestamp(datetime.fromisoformat(manifest[k])) for k in
        ("train_label_end", "calibration_start", "calibration_label_end", "final_test_start", "final_test_end")]
    if not dates[0] < dates[1] <= dates[2] < dates[3] <= dates[4]: raise ValueError("Invalid chronological candidate partitions")
    spec = spec | {"verified_dataset_end": dataset_dates.iloc[-1].isoformat()}
    return manifest, spec


def comparison_prediction(directory, candles, *, observed_at, fee_rate, slippage_rate, source_claim, manifest_sha256, expected_horizon=24, minimum_history=8760):
    manifest, spec = load_comparison_model(directory, fee_rate=fee_rate, slippage_rate=slippage_rate, source_claim=source_claim, manifest_sha256=manifest_sha256, expected_horizon=expected_horizon, minimum_history=minimum_history)
    clock = utc_timestamp(observed_at)
    rows = list(candles)
    _check(rows, clock, 51)
    if any(r.instrument_id != manifest["instrument"] or r.timeframe != "1h" for r in rows): raise ValueError("Wrong candle identity")
    opened = utc_timestamp(datetime.fromisoformat(rows[-1].opened_at))
    if max(utc_timestamp(datetime.fromisoformat(manifest["final_test_end"])), utc_timestamp(datetime.fromisoformat(spec["verified_dataset_end"]))) >= opened:
        raise ValueError("Forward comparison must start after final research test")
    if not 0 <= (clock - opened - timedelta(hours=1)).total_seconds() <= 300: raise ValueError("Late inference observation")
    frame = pd.DataFrame({"date": [r.opened_at for r in rows], "close": [float(r.close) for r in rows], "volume": [float(r.volume) for r in rows]})
    features = generate_features(frame, version="2").iloc[[-1]][FEATURES]
    if not np.isfinite(features).all().all() or (features.abs() > 1e12).any().any():
        raise ValueError("Feature numerical domain exceeded")
    with np.errstate(all="raise"):
        probability = float(predict_portable(spec, features)[0])
    expected = probability * spec["mean_win"] - (1 - probability) * spec["mean_loss"]
    return {"ml_model_id": manifest["run_id"] + ":" + manifest_sha256, "ml_probability": probability, "ml_expected_net": expected,
            "horizon_hours": manifest["horizon_bars"], "eligible_for_trading": False,
            "ood_gate": "unavailable_no_training_ranges", "source_claim_verified": False}
