"""Read isolated comparison state without creating or modifying its database."""
import argparse
import json
from pathlib import Path
import sqlite3


def report(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected existing regular comparison database')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        config = connection.execute('SELECT payload FROM comparison_config WHERE id=1').fetchone()
        last = connection.execute('SELECT report FROM comparison_steps ORDER BY bar_close DESC LIMIT 1').fetchone()
        audit = connection.execute('SELECT observed_at,status,reason FROM comparison_audits ORDER BY id DESC LIMIT 1').fetchone()
        return {'config': json.loads(config[0]), 'latest_step': json.loads(last[0]) if last else None,
                'latest_blocked_observation': audit, 'eligible_for_qualification': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    print(json.dumps(report(parser.parse_args().database), sort_keys=True))
