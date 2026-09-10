"""Read-only evaluation of a frozen forward experiment; never authorizes trading."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from app.services.paper_comparison import STRATEGIES, canonical, money, number, _initial_state, ComparisonConfig


def _time(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone required')
    return result.astimezone(timezone.utc)


def _path(root, value):
    candidate = root / value
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('Ledger must remain inside experiment directory')
    return candidate


def _ledger(path, arm, start, end, clock, expected):
    if not path.exists():
        return dict(status='missing', observations=0, coverage=0, missing_hours=expected, strategies={})
    if not path.is_file():
        raise ValueError('Ledger must be a regular file')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN')
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        config = connection.execute('SELECT fingerprint,payload FROM comparison_config WHERE id=1').fetchone()
        payload = json.loads(config['payload'])
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        model_id = arm['comparison_config']['ml_model_id']
        if model_id != arm['model_run_id']+':'+arm['manifest_sha256']:
            raise ValueError('Invalid frozen model pin binding')
        if digest != config['fingerprint'] or digest != arm['config_sha256'] or payload['ml_model_id'] != model_id:
            raise ValueError('Ledger configuration/model differs from frozen experiment')
        fields = ComparisonConfig.__dataclass_fields__
        validated = ComparisonConfig(**{key: payload[key] for key in fields}).validated()
        if canonical(validated.identity) != canonical(payload):
            raise ValueError('Unsupported comparison configuration')
        initial = number(payload['starting_cash'])
        states = {name: dict(cash=initial, quantity=Decimal(0), fees=Decimal(0), fills=0,
                            peak=initial, drawdown=Decimal(0), equity=initial, open_cost=None, pnls=[]) for name in STRATEGIES}
        stamps, datasets = [], {}
        last_accounts = {name: _initial_state(validated) for name in STRATEGIES}
        previous = None
        for stored in connection.execute('SELECT * FROM comparison_steps ORDER BY bar_close'):
            stamp, observed = _time(stored['bar_close']), _time(stored['observed_at'])
            if stamp < start or stamp >= end:
                raise ValueError('Ledger contains observations outside frozen window')
            if stamp.minute or stamp.second or stamp.microsecond or not 0 <= (observed-stamp).total_seconds() <= 300:
                raise ValueError('Invalid forward observation timestamp')
            if observed > clock or (previous is not None and stamp <= previous):
                raise ValueError('Future or nonmonotonic observation')
            report = json.loads(stored['report'])
            if (report['bar_close'] != stored['bar_close'] or report['observed_at'] != stored['observed_at']
                    or report['data_sha256'] != stored['data_sha256'] or report['config_sha256'] != digest
                    or report['ml_model_id'] != model_id or report['status'] != 'observed'):
                raise ValueError('Ledger report binding mismatch')
            accounts = report['accounts']
            if len(accounts) != len(STRATEGIES) or {a['strategy'] for a in accounts} != set(STRATEGIES):
                raise ValueError('Incomplete strategy ledger')
            for account in accounts:
                state = states[account['strategy']]
                fill = account['fill']
                if fill:
                    qty, price, fee = (number(fill[key]) for key in ('quantity', 'price', 'fee'))
                    if qty <= 0 or price <= 0 or fee != money(money(qty*price)*number(payload['fee_rate'])):
                        raise ValueError('Invalid fill amounts/fees')
                    if previous is None or stamp-previous != timedelta(hours=1) or _time(fill['decision_bar_close']) != previous:
                        raise ValueError('Fill lacks preceding consecutive decision')
                    pending = last_accounts[account['strategy']]['pending']
                    if pending is None or pending['side'] != fill['side'] or _time(pending['bar_close']) != previous:
                        raise ValueError('Fill differs from preceding pending decision')
                    cost = money(qty*price)
                    if fill['side'] == 'buy' and state['quantity'] == 0:
                        state['cash'] -= cost+fee
                        state['quantity'] = qty
                        state['open_cost'] = cost+fee
                    elif fill['side'] == 'sell' and qty == state['quantity'] and state['open_cost'] is not None:
                        state['cash'] += cost-fee
                        state['quantity'] = Decimal(0)
                        state['pnls'].append(cost-fee-state['open_cost'])
                        state['open_cost'] = None
                    else:
                        raise ValueError('Unpaired or invalid fill')
                    state['fees'] += fee
                    state['fills'] += 1
                committed = account['state']
                if (number(committed['cash']) != state['cash'] or number(committed['quantity']) != state['quantity']
                        or number(committed['fees_paid']) != state['fees'] or committed['fills'] != state['fills']):
                    raise ValueError('Account balances do not reconcile to fills')
                equity = number(account['equity'])
                if equity < state['cash'] or (state['quantity'] == 0 and equity != state['cash']):
                    raise ValueError('Invalid mark-to-market equity')
                state['equity'] = equity
                state['peak'] = max(state['peak'], equity)
                state['drawdown'] = max(state['drawdown'], (state['peak']-equity)/state['peak'])
                if number(committed['peak_equity']) != state['peak'] or number(committed['maximum_drawdown']) != state['drawdown']:
                    raise ValueError('Drawdown does not reconcile')
            previous = stamp
            last_accounts = {a['strategy']: a['state'] for a in accounts}
            stamps.append(stamp)
            datasets[stamp.isoformat()] = stored['data_sha256']
        current = {row['strategy']: json.loads(row['state']) for row in connection.execute('SELECT * FROM comparison_accounts')}
        if current != last_accounts:
            raise ValueError('Latest account snapshot differs from ledger')
        summaries = {}
        for name, state in states.items():
            wins = sum((p for p in state['pnls'] if p > 0), Decimal(0))
            losses = -sum((p for p in state['pnls'] if p < 0), Decimal(0))
            summaries[name] = dict(roundtrips=len(state['pnls']), realized_net_pnl=float(sum(state['pnls'], Decimal(0))),
                net_equity_pnl=float(state['equity']-initial), equity=float(state['equity']), fees_paid=float(state['fees']),
                open_quantity=float(state['quantity']), maximum_drawdown=float(state['drawdown']),
                gross_winning_net_pnl=float(wins), gross_losing_net_pnl=float(losses),
                profit_factor=float(wins/losses) if losses else None, no_losing_roundtrips=losses == 0,
                halted=bool(current.get(name, {}).get('halted', False)))
        due = sum(stamp <= clock-timedelta(minutes=5) for stamp in stamps)
        return dict(status='observed' if stamps else 'empty', observations=len(stamps), due_observations=due,
                    expected_hours=expected, missing_hours=max(0, expected-due), coverage=due/expected if expected else 0,
                    latest_bar_close=stamps[-1].isoformat() if stamps else None,
                    fresh=bool(stamps and clock-stamps[-1] <= timedelta(hours=1, minutes=5)),
                    strategies=summaries, data_hashes=datasets)
    finally:
        connection.close()


def evaluate_experiment(root, observed_at):
    from app.services.forward_experiment import load_contract, forecast_summary
    root = Path(root)
    manifest = load_contract(root)
    policy = manifest['policy']
    start, end, clock = map(_time, (policy['start_at'], policy['end_at'], observed_at))
    if any(t.minute or t.second or t.microsecond for t in (start, end)):
        raise ValueError('Frozen boundaries must be hourly')
    if (type(policy['minimum_days']) is not int or policy['minimum_days'] < 1
            or type(policy['minimum_roundtrips']) is not int or policy['minimum_roundtrips'] < 1
            or not 0 < number(policy['minimum_coverage']) <= 1
            or not 0 < number(policy['maximum_drawdown']) <= 1
            or number(policy['minimum_profit_factor']) < 1):
        raise ValueError('Invalid evaluation policy')
    if end <= start or (end-start).total_seconds() < policy['minimum_days']*86400:
        raise ValueError('Invalid frozen evaluation duration')
    due_end = min(end-timedelta(hours=1), clock-timedelta(minutes=5))
    expected = max(0, int((due_end-start).total_seconds()//3600)+1)
    required = {f'{role}_{cost}' for role in ('champion','challenger') for cost in ('baseline','stress')}
    if set(manifest['arms']) != required:
        raise ValueError('Exactly four frozen experiment arms required')
    arms = {name: _ledger(_path(root, arm['ledger_path']), arm, start, end, clock, expected)
            for name, arm in manifest['arms'].items()}
    checks = {}
    for cost in ('baseline','stress'):
        challenger, champion = arms[f'challenger_{cost}'], arms[f'champion_{cost}']
        overlap = set(challenger.get('data_hashes', {})) & set(champion.get('data_hashes', {}))
        if any(challenger['data_hashes'][stamp] != champion['data_hashes'][stamp] for stamp in overlap):
            raise ValueError('Paired arms observed different candle data')
        ml = challenger['strategies'].get('cost_ml', {})
        other = champion['strategies'].get('cost_ml', {})
        checks[cost] = dict(coverage=all(a['coverage'] >= policy['minimum_coverage'] for a in (challenger, champion)),
            matched_observations=set(challenger.get('data_hashes',{})) == set(champion.get('data_hashes',{})),
            minimum_roundtrips=ml.get('roundtrips',0) >= policy['minimum_roundtrips'],
            positive_realized_pnl=ml.get('realized_net_pnl',0) > 0,
            positive_equity_pnl=ml.get('net_equity_pnl',0) > 0,
            drawdown=bool(ml) and ml['maximum_drawdown'] <= policy['maximum_drawdown'] and not ml['halted'],
            profit_factor=bool(ml) and ml['gross_winning_net_pnl'] > 0 and ml['gross_winning_net_pnl'] >= policy['minimum_profit_factor']*ml['gross_losing_net_pnl'],
            beats_champion=bool(ml and other) and ml['net_equity_pnl'] > other['net_equity_pnl'],
            beats_buy_hold=bool(ml) and ml['net_equity_pnl'] > challenger['strategies']['buy_hold']['net_equity_pnl'],
            beats_trend=bool(ml) and ml['net_equity_pnl'] > challenger['strategies']['trend']['net_equity_pnl'],
            beats_mean_reversion=bool(ml) and ml['net_equity_pnl'] > challenger['strategies']['mean_reversion']['net_equity_pnl'])
    complete = clock >= end
    passed = complete and all(all(items.values()) for items in checks.values())
    for arm in arms.values():
        arm.pop('data_hashes', None)
    return dict(version='forward-evaluation-v1', experiment_id=manifest['experiment_id'], observed_at=clock.isoformat(),
        start_at=start.isoformat(), end_at=end.isoformat(), status='review_candidate' if passed else ('inconclusive' if complete else 'accumulating'),
        window_complete=complete, checks=checks, arms=arms, eligible_for_live_trading=False, live_authorized=False,
        probability_diagnostics=forecast_summary(root),
        limitations=['Hourly close-proxy simulation is not brokerage execution evidence.',
                     'Accounting is reconciled to recorded fills; source quotes are not independently replayed.',
                     'Open positions remain marked, not forcibly liquidated; exit costs are not reserved in equity.',
                     'No automatic promotion or live authorization; independent review and explicit approval remain required.'])


def render_markdown(report):
    lines = ['# Forward experiment evaluation', '', f"Status: **{report['status']}**", '',
             f"Experiment: `{report['experiment_id']}`", f"As of: {report['observed_at']}",
             f"Frozen window: {report['start_at']} to {report['end_at']} (end exclusive)", '',
             '| Arm | Coverage | Missing hours | ML roundtrips | Realized net $ | Equity net $ | Max drawdown |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for name, arm in report['arms'].items():
        ml = arm['strategies'].get('cost_ml', {})
        lines.append(f"| {name} | {arm['coverage']:.1%} | {arm['missing_hours']} | {ml.get('roundtrips',0)} | {ml.get('realized_net_pnl',0):.4f} | {ml.get('net_equity_pnl',0):.4f} | {ml.get('maximum_drawdown',0):.2%} |")
    lines += ['', '## Frozen checks', '']
    for cost, checks in report['checks'].items():
        lines.append(f"- {cost}: " + ', '.join(f"{name}={'pass' if value else 'not met'}" for name,value in checks.items()))
    probability = report.get('probability_diagnostics', {})
    lines += ['', '## Probability diagnostics', '']
    if 'arms' in probability:
        for name, values in probability['arms'].items():
            scored = values['scored']
            lines.append(f"- {name}: {values['recorded']} predictions recorded; {scored['rows']} scored; metrics: {json.dumps(scored.get('metrics'), sort_keys=True)}")
        lines += ['', probability.get('label_proxy', ''), '', 'Probability diagnostics are descriptive and do not change the frozen gates.']
    else:
        lines.append(probability.get('status', 'unavailable'))
    lines += ['', '## Limitations', ''] + ['- '+item for item in report['limitations']]
    return '\n'.join(lines)+'\n'
