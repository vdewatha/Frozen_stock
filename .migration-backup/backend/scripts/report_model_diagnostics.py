"""Generate an immutable offline report from an explicitly pinned candidate."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.model_diagnostics import build_report, publish_report, render_markdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--folds', type=int, choices=range(2, 6), default=3)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.model.resolve()):
        parser.error('Output must be outside the immutable model directory')
    report = build_report(args.model, manifest_sha256=args.manifest_sha256, folds=args.folds)
    print(publish_report(report, args.output, render_markdown(report)))


if __name__ == '__main__':
    main()
