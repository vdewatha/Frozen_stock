"""Disposable real Freqtrade dry-run buy/sell acceptance; never live or qualifying.

Uses a synthetic binding/decision fixture solely to exercise the gateway. No
existing database, exchange credentials, host port, or model approval is used.
"""
import json
import os
from pathlib import Path
import secrets
import time
from uuid import uuid4

from smoke_freqtrade import IMAGE, BOOTSTRAP, command
from app.integrations.paper_venue import kraken_execution_config

ROUNDTRIP = r'''
import json, sys, time, urllib.request
from datetime import datetime, timezone
from decimal import Decimal
sys.path[:0]=['/project','/project/tests']
from sqlalchemy.orm import Session, sessionmaker
from app.integrations.freqtrade_execution import FreqtradeDryRunClient
from app.models.shadow import ShadowDecision
from app.models.execution import PaperExecutionAccount, PaperExecutionFill
from app.services.paper_execution import execution_transaction, reserve_intent
from app.services.freqtrade_dispatch import dispatch_intent, reconcile_intent
from test_paper_execution import PaperExecutionTests
config=json.load(open('/tmp/probe-config.json'))['api_server']
fixture=PaperExecutionTests()
fixture.setUp()
factory=sessionmaker(fixture.engine,autoflush=False)
def quote(side):
    with urllib.request.urlopen('https://api.kraken.com/0/public/Ticker?pair=XBTUSD',timeout=15) as response:
        payload=json.load(response)
    assert payload['error']==[]
    ticker=next(iter(payload['result'].values()))
    raw=Decimal(ticker['a' if side=='buy' else 'b'][0])
    return (raw*(Decimal('1.002') if side=='buy' else Decimal('.998'))).quantize(Decimal('.1'))
def reserve(side,ident,quantity):
    price=quote(side)
    fixture.now=datetime.now(timezone.utc)
    with Session(fixture.engine) as db, execution_transaction(db):
        decision=db.get(ShadowDecision,ident)
        if decision is None:
            decision=fixture.decision(ident,side)
            db.add(decision)
        decision.reference_price=float(price)
        decision.observed_at=fixture.now.replace(tzinfo=None)
        db.flush()
        return reserve_intent(db,decision_id=ident,approval_id=fixture.approval_id,
            side=side,quantity=quantity,limit_price=price,client_order_id='acceptance-'+side).id
try:
    with FreqtradeDryRunClient('http://127.0.0.1:8080',config['username'],config['password']) as client:
        client.verify()
        assert client.trades()==[]
        results=[]
        for side,ident in [('buy',1),('sell',2)]:
            intent=reserve(side,ident,Decimal('.001'))
            result=dispatch_intent(factory,client,intent)  # Never retry a POST.
            deadline=time.monotonic()+90
            while result['status']!='reconciled' and time.monotonic()<deadline:
                time.sleep(2)
                result=reconcile_intent(factory,client,intent)
            assert result['status']=='reconciled',result
            with Session(fixture.engine) as db:
                before=db.query(PaperExecutionFill).count()
            assert reconcile_intent(factory,client,intent)['status']=='reconciled'
            with Session(fixture.engine) as db:
                assert db.query(PaperExecutionFill).count()==before
            results.append(result)
        with Session(fixture.engine) as db:
            account=db.get(PaperExecutionAccount,1)
            assert account.quantity==account.reserved_quantity==account.reserved_cash==0
            assert db.query(PaperExecutionFill).count()==2
            cash=str(account.cash)
        assert client.trades()==[]
        print(json.dumps({'ok':True,'dry_run':True,'eligible_for_qualification':False,
            'synthetic_decisions':True,'actual_provider':'Freqtrade 2026.8 / Kraken public data',
            'roundtrip':results,'starting_cash':'1000','ending_cash':cash,'duplicate_reconciliation_verified':True}))
finally:
    fixture.tearDown()
'''


def main():
    name = 'trading-order-acceptance-' + uuid4().hex[:12]
    password, jwt = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    environment = dict(os.environ, PAPER_PROBE_CONFIG=json.dumps(kraken_execution_config('acceptance', password, jwt)))
    backend = Path(__file__).resolve().parents[1]
    try:
        result = command(['docker','run','-d','--pull','never','--name',name,
            '--entrypoint','python','--env','PAPER_PROBE_CONFIG','--memory','2g','--cpus','2',
            '--pids-limit','256','--cap-drop','ALL','--security-opt','no-new-privileges','--read-only',
            '--tmpfs','/tmp','--tmpfs','/freqtrade/user_data:uid=1000,gid=1000,mode=0700',
            '--mount',f'type=bind,src={backend / "freqtrade_strategies"},dst=/strategies,readonly',
            '--mount',f'type=bind,src={backend / "app"},dst=/project/app,readonly',
            '--mount',f'type=bind,src={backend / "tests" / "test_paper_execution.py"},dst=/project/tests/test_paper_execution.py,readonly',
            IMAGE,'-c',BOOTSTRAP],timeout=30,env=environment)
        if result.returncode:
            raise RuntimeError('Disposable dry-run container could not start')
        deadline=time.monotonic()+120
        ready="import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/ping',timeout=2)"
        while time.monotonic()<deadline:
            if command(['docker','exec',name,'python','-c',ready]).returncode==0:
                break
            time.sleep(2)
        else:
            raise RuntimeError('Dry-run API readiness timed out')
        # The mutating acceptance body executes exactly once, even on failure.
        result=command(['docker','exec',name,'python','-c',ROUNDTRIP],timeout=240)
        print((result.stdout+result.stderr).replace(password,'[redacted]').replace(jwt,'[redacted]'))
        return result.returncode
    finally:
        command(['docker','rm','--force','--volumes',name])


if __name__=='__main__':
    raise SystemExit(main())
