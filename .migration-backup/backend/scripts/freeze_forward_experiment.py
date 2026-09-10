"""Freeze an 84-day prospective champion/challenger paper experiment."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.forward_experiment import freeze_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pins', type=Path, required=True, help='JSON mapping four arms to directory and manifest_sha256')
    parser.add_argument('--root', type=Path, required=True, help='New, separate experiment directory')
    parser.add_argument('--start-at', required=True, help='Future aligned hour with explicit timezone')
    parser.add_argument('--runtime-image-id', required=True, help='Exact built Docker image SHA-256')
    args = parser.parse_args()
    pins = json.loads(args.pins.read_text())
    for pin in pins.values():
        if args.root.resolve().is_relative_to(Path(pin['directory']).resolve()):
            parser.error('Experiment output cannot be inside a model directory')
    manifest = freeze_experiment(args.root, pins, start_at=datetime.fromisoformat(args.start_at), runtime_image_id=args.runtime_image_id)
    print(json.dumps({'experiment_id': manifest['experiment_id'], 'policy': manifest['policy']}))


if __name__ == '__main__':
    main()
