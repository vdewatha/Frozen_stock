"""Offline Colab-compatible research job; never registers models or accesses brokers."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

def run(source, pin, output, profile, family):
    import pandas as pd
    from app.services.comparison_model import _read, _decode, load_comparison_model
    from app.services.research_training_v3 import train_research_v3
    from app.services.model_diagnostics import build_report, publish_report, render_markdown
    source, output = Path(source), Path(output)
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output must be outside the source artifact")
    costs = {"baseline": (.008, .001), "stress": (.01, .002)}
    if profile not in costs or family not in ("random_forest", "auto"):
        raise ValueError("Unknown fixed job policy")
    declared = _decode(_read(source / "manifest.json", 2*1024*1024))
    manifest, _ = load_comparison_model(source, manifest_sha256=pin,
        source_claim=declared["source_claim"], fee_rate=declared["fee_rate_per_side"],
        slippage_rate=declared["slippage_rate_per_side"], expected_horizon=declared["horizon_bars"])
    snapshot = _read(source / "dataset.csv", 64*1024*1024)
    if hashlib.sha256(snapshot).hexdigest() != manifest["dataset_sha256"]:
        raise ValueError("Dataset changed after validation")
    if output.exists():
        raise FileExistsError("Use a fresh job directory; completed evidence is immutable")
    output.mkdir(parents=True)
    fee, slip = costs[profile]
    candidate = train_research_v3(pd.read_csv(io.BytesIO(snapshot)), output / "candidates",
        source=manifest["source_claim"], horizon=manifest["horizon_bars"], fee_rate=fee,
        slippage_rate=slip, seed=manifest["seed"], gap_policy=manifest.get("gap_policy", "strict"),
        fixed_model=None if family == "auto" else family, source_snapshot=snapshot)
    model = output / "candidates" / candidate["run_id"]
    model_pin = hashlib.sha256((model / "manifest.json").read_bytes()).hexdigest()
    report = build_report(model, manifest_sha256=model_pin)
    report_dir = publish_report(report, output / "diagnostics", render_markdown(report))
    job = dict(profile=profile, family=family, source_manifest_sha256=pin,
        candidate_run_id=candidate["run_id"], candidate_manifest_sha256=model_pin,
        diagnostic_report_id=report["report_id"], eligible_for_trading=False,
        live_authorized=False, note="Previously examined history; not new forward evidence")
    (output / "job.json").write_text(json.dumps(job, indent=2))
    files = [model / n for n in ("manifest.json", "dataset.csv", "calibrated_model.json", "final_test_predictions.csv")]
    files += [report_dir / n for n in ("report.json", "report.md", "files.json")]
    files += [output / "job.json"]
    hashes = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (output / "export-hashes.json").write_text(json.dumps(hashes, indent=2, sort_keys=True))
    archive = output / "research-export.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as bundle:
        for path in files + [output / "export-hashes.json"]:
            bundle.write(path, str(path.relative_to(output)))
    return job | {"archive": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("baseline", "stress"), required=True)
    parser.add_argument("--family", choices=("random_forest", "auto"), default="random_forest")
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.manifest_sha256, args.output, args.profile, args.family)))

