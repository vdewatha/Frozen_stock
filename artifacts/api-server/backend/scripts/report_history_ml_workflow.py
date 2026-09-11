"""Read-only historical download and pinned-model status."""
import argparse
import json
from pathlib import Path
import sqlite3


def report(root):
    root = Path(root)
    database = root / 'backfill/trade-backfill.sqlite'
    if database.is_symlink() or not database.is_file():
        raise ValueError('Expected existing regular backfill database')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        state = dict(connection.execute('SELECT * FROM backfill_state WHERE id=1').fetchone())
        hours = connection.execute('SELECT COUNT(*) FROM backfill_hours').fetchone()[0]
        latest_error = connection.execute('SELECT observed_at,reason FROM backfill_errors ORDER BY id DESC LIMIT 1').fetchone()
    result = {'phase': 'raw_download_complete_validation_pending' if state['complete'] else 'historical_backfill',
              'state': state, 'provisional_hours': hours, 'required_hours': 8760,
              'database_bytes': database.stat().st_size, 'latest_error': dict(latest_error) if latest_error else None,
              'eligible_for_trading': False}
    provenance = root / 'backfill/provenance.json'
    if provenance.exists():
        if provenance.is_symlink() or not provenance.is_file() or provenance.stat().st_size > 1048576:
            raise ValueError('Invalid provenance file')
        metadata = json.loads(provenance.read_text())
        result.update(phase='history_exported_training_pending',
            exported_observed_hours=metadata.get('rows'), declared_no_trade_hours=metadata.get('missing_hours', []),
            gap_policy=metadata.get('gap_policy', 'strict'))
    selection = root / 'pinned_models.json'
    if selection.exists():
        if selection.is_symlink() or not selection.is_file() or selection.stat().st_size > 1048576:
            raise ValueError('Invalid pinned selection file')
        result['pinned_models'] = json.loads(selection.read_text())
        result['phase'] = 'models_pinned_check_forward_comparison_separately'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    print(json.dumps(report(parser.parse_args().output), sort_keys=True))
