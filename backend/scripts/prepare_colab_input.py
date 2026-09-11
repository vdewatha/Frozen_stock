"""Package only four verified research files for a manual private Colab upload."""
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.comparison_model import _read, _decode, load_comparison_model


def package(model, pin, output):
    model, output = Path(model), Path(output)
    if output.resolve().is_relative_to(model.resolve()):
        raise ValueError('Output must be outside the immutable model')
    payload = _read(model/'manifest.json', 2*1024*1024)
    if hashlib.sha256(payload).hexdigest() != pin:
        raise ValueError('Manifest pin mismatch')
    declared = _decode(payload)
    manifest, _ = load_comparison_model(model, manifest_sha256=pin, source_claim=declared['source_claim'],
        fee_rate=declared['fee_rate_per_side'], slippage_rate=declared['slippage_rate_per_side'],
        expected_horizon=declared['horizon_bars'])
    files = {'manifest.json': payload}
    for name in ('dataset.csv', 'calibrated_model.json', 'final_test_predictions.csv'):
        value = _read(model/name, 64*1024*1024)
        if hashlib.sha256(value).hexdigest() != manifest['files'][name]:
            raise ValueError('Artifact changed during packaging')
        files[name] = value
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print('Bundle SHA-256:', package(args.model, args.manifest_sha256, args.output))
