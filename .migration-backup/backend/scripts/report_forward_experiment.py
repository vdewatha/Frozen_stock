"""Read frozen paper-experiment evidence without advancing or changing trading."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.forward_evaluation import evaluate_experiment, render_markdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = evaluate_experiment(args.experiment, datetime.now(timezone.utc))
    content = json.dumps(report, sort_keys=True, indent=2, allow_nan=False)+'\n'
    if args.output is None:
        print(content, end='')
        return
    if args.output.resolve().is_relative_to((args.experiment/'evidence').resolve()):
        parser.error('Report output must be outside evidence directory')
    destination = args.output/hashlib.sha256(content.encode()).hexdigest()
    destination.mkdir(parents=True, exist_ok=False)
    (destination/'report.json').write_text(content)
    (destination/'report.md').write_text(render_markdown(report))
    print(destination)


if __name__ == '__main__':
    main()
