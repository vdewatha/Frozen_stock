"""Opt-in disposable real dry-run broker + gateway restart acceptance.

Deliberately lose one successful POST response, persist unknown, restart the
broker and gateway, recover by provider evidence, then close the simulated trade.
"""
import json
import os
from pathlib import Path
import secrets
import time
from uuid import uuid4

from accept_freqtrade import ROUNDTRIP
from smoke_freqtrade import IMAGE, BOOTSTRAP, command
from app.integrations.paper_venue import kraken_execution_config

COMMON = ROUNDTRIP.split("try:\n    with FreqtradeDryRunClient", 1)[0]
BEFORE = COMMON + r'''
import sqlite3
from app.models.execution import PaperOrderIntent
from app.services.paper_execution import PaperExecutionError
try:
    with FreqtradeDryRunClient('http://127.0.0.1:8080',config['username'],config['password']) as client:
        assert client.trades()==[]
        intent=reserve('buy',1,Decimal('.001'))
        real_enter=client.enter
        def lose_response(**kwargs):
            real_enter(**kwargs)  # Exactly one actual paper POST.
            raise TimeoutError('Injected loss of acknowledgement')
        client.enter=lose_response
        try:
            dispatch_intent(factory,client,intent)
            raise AssertionError('Injected ambiguity did not persist')
        except PaperExecutionError:
            pass
        with Session(fixture.engine) as db:
            assert db.get(PaperOrderIntent,intent).status=='unknown'
        source=sqlite3.connect(fixture.engine.url.database)
        destination=sqlite3.connect('/freqtrade/user_data/restart-gateway.sqlite')
        source.backup(destination)
        destination.close(); source.close()
        print('Unknown intent persisted after one accepted dry-run entry.')
finally:
    fixture.tearDown()
'''
AFTER = COMMON + r'''
from sqlalchemy import create_engine
from app.models.execution import PaperOrderIntent
from app.services.paper_recovery import run_recovery_cycle
fixture.engine.dispose()
fixture.engine=create_engine('sqlite:////freqtrade/user_data/restart-gateway.sqlite')
factory=sessionmaker(fixture.engine,autoflush=False)
try:
    with FreqtradeDryRunClient('http://127.0.0.1:8080',config['username'],config['password']) as client:
        def never_enter(**kwargs):
            raise AssertionError('Recovery must never resubmit the entry')
        client.enter=never_enter
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            result=run_recovery_cycle(factory,client)
            with Session(fixture.engine) as db:
                recovered=db.query(PaperOrderIntent).filter_by(side='buy').one()
                done=recovered.status=='reconciled'
            if done: break
            time.sleep(2)
        assert done,result
        assert result['status']=='success',result
        with Session(fixture.engine) as db:
            assert db.get(PaperExecutionAccount,1).quantity==Decimal('.001')
            assert db.query(PaperExecutionFill).count()==1
        assert run_recovery_cycle(factory,client)['status']=='success'
        sale=reserve('sell',2,Decimal('.001'))
        result=dispatch_intent(factory,client,sale)
        deadline=time.monotonic()+90
        while result['status']!='reconciled' and time.monotonic()<deadline:
            time.sleep(2)
            result=reconcile_intent(factory,client,sale)
        assert result['status']=='reconciled',result
        with Session(fixture.engine) as db:
            account=db.get(PaperExecutionAccount,1)
            assert account.quantity==account.reserved_quantity==account.reserved_cash==0
            assert db.query(PaperExecutionFill).count()==2
        assert client.trades()==[]
        print(json.dumps({'ok':True,'dry_run':True,'synthetic_decisions':True,
            'eligible_for_qualification':False,'broker_restart':True,'gateway_restart':True,
            'lost_acknowledgement_recovered':True,'entry_posts':1,'exit_posts':1,'fills':2,
            'ending_inventory':'0'}))
finally:
    fixture.tearDown()
'''


def ready(name):
    deadline = time.monotonic() + 120
    probe = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/ping',timeout=2)"
    while time.monotonic() < deadline:
        if command(['docker', 'exec', name, 'python', '-c', probe]).returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError('Disposable restart API readiness timed out')


def main():
    name = 'trading-restart-acceptance-' + uuid4().hex[:12]
    volume = name + '-state'
    password, jwt = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    environment = dict(os.environ, PAPER_PROBE_CONFIG=json.dumps(kraken_execution_config('restart', password, jwt)))
    backend = Path(__file__).resolve().parents[1]
    bootstrap = BOOTSTRAP.replace('sqlite:////tmp/probe.dryrun.sqlite', 'sqlite:////freqtrade/user_data/restart-provider.sqlite')
    created = False
    try:
        if command(['docker', 'volume', 'create', volume]).returncode:
            raise RuntimeError('Disposable volume creation failed')
        created = True
        result = command(['docker','run','-d','--pull','never','--name',name,
            '--entrypoint','python','--env','PAPER_PROBE_CONFIG','--memory','2g','--cpus','2',
            '--pids-limit','256','--cap-drop','ALL','--security-opt','no-new-privileges','--read-only',
            '--tmpfs','/tmp','--mount',f'type=volume,src={volume},dst=/freqtrade/user_data',
            '--mount',f'type=bind,src={backend / "freqtrade_strategies"},dst=/strategies,readonly',
            '--mount',f'type=bind,src={backend / "app"},dst=/project/app,readonly',
            '--mount',f'type=bind,src={backend / "tests" / "test_paper_execution.py"},dst=/project/tests/test_paper_execution.py,readonly',
            IMAGE,'-c',bootstrap],timeout=30,env=environment)
        if result.returncode:
            raise RuntimeError('Disposable restart container could not start')
        for index, body in enumerate((BEFORE, AFTER)):
            if index:
                if command(['docker','restart','--time','30',name], timeout=60).returncode:
                    raise RuntimeError('Disposable broker restart failed')
            ready(name)
            # Each mutating phase executes once, never retried on any failure.
            result = command(['docker','exec',name,'python','-c',body], timeout=240)
            print((result.stdout + result.stderr).replace(password,'[redacted]').replace(jwt,'[redacted]'))
            if result.returncode:
                return result.returncode
        return 0
    finally:
        command(['docker','rm','--force','--volumes',name])
        if created:
            command(['docker','volume','rm',volume])


if __name__ == '__main__':
    raise SystemExit(main())
