"""Train a fixed RF research challenger from an explicitly pinned existing dataset."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.services.comparison_model import _read, _decode, load_comparison_model
from app.services.research_training_v3 import train_research_v3


def train_challenger(model, manifest_sha256, output):
    model, output = Path(model), Path(output)
    if output.resolve().is_relative_to(model.resolve()):
        raise ValueError('Output must be outside the immutable source model directory')
    payload = _read(model / 'manifest.json', 2 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != manifest_sha256:
        raise ValueError('Pinned manifest hash mismatch')
    source = _decode(payload)
    manifest, _ = load_comparison_model(model,
        manifest_sha256=manifest_sha256, source_claim=source['source_claim'],
        fee_rate=source['fee_rate_per_side'], slippage_rate=source['slippage_rate_per_side'],
        expected_horizon=source['horizon_bars'])
    dataset = _read(model / 'dataset.csv', 64 * 1024 * 1024)
    if hashlib.sha256(dataset).hexdigest() != manifest['dataset_sha256']:
        raise ValueError('Pinned dataset changed during loading')
    return train_research_v3(pd.read_csv(io.BytesIO(dataset)), output,
        source=manifest['source_claim'], horizon=manifest['horizon_bars'],
        fee_rate=manifest['fee_rate_per_side'], slippage_rate=manifest['slippage_rate_per_side'],
        seed=manifest['seed'], gap_policy=manifest.get('gap_policy', 'strict'),
        fixed_model='random_forest', source_snapshot=dataset)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = train_challenger(args.model, args.manifest_sha256, args.output)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
