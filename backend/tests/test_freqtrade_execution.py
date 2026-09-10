import unittest
from decimal import Decimal
from unittest.mock import Mock
import httpx
from sqlalchemy.orm import Session, sessionmaker
import test_paper_execution as fixtures
from app.integrations.freqtrade import FreqtradeError
from app.integrations.freqtrade_execution import FreqtradeDryRunClient
from app.models.execution import PaperOrderIntent, PaperExecutionAccount
from app.services.paper_execution import execution_transaction, PaperExecutionError
from app.services.freqtrade_dispatch import dispatch_intent, reconcile_intent


class TransportTests(unittest.TestCase):
    def test_live_mode_never_posts(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200,json={"dry_run":False})
        with FreqtradeDryRunClient("http://localhost:8080","u","p",transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(FreqtradeError): client.enter(price=Decimal(100),quantity=Decimal(1),tag="test")
        self.assertEqual(calls,["GET"])

    def test_pinned_contract_payload(self):
        requests=[]
        def handler(request):
            requests.append(request)
            if request.method=="GET":
                if request.url.path.endswith("whitelist"):
                    return httpx.Response(200,json={"whitelist":["BTC/USD"]})
                return httpx.Response(200,json=dict(dry_run=True,exchange="kraken",timeframe="1h",trading_mode="spot",version="2026.8",force_entry_enable=True,
                    bot_name="kraken-paper-execution",strategy="ObservationOnly",position_adjustment_enable=False,short_allowed=False,state="running"))
            return httpx.Response(200,json={"trade_id":1})
        with FreqtradeDryRunClient("http://localhost:8080","u","p",transport=httpx.MockTransport(handler)) as client:
            client.enter(price=Decimal(100),quantity=Decimal(2),tag="paper:1")
        self.assertEqual(requests[-1].url.path,"/api/v1/forceenter")
        self.assertIn(b'"stakeamount": 200.0',requests[-1].content)


class DispatchTests(unittest.TestCase):
    setUp=fixtures.PaperExecutionTests.setUp
    tearDown=fixtures.PaperExecutionTests.tearDown
    decision=fixtures.PaperExecutionTests.decision
    reserve=fixtures.PaperExecutionTests.reserve

    def prepare(self):
        with Session(self.engine) as db, execution_transaction(db):
            ident=self.reserve(db).id
        self.factory=sessionmaker(self.engine, autoflush=False)
        self.order=dict(pair="BTC/USD",order_id="dry1",ft_order_side="buy",order_type="limit",amount=2,
                        filled=1,cost=100,safe_price=100,status="open",is_open=True,ft_fee_base=0)
        self.trade=dict(trade_id=10,pair="BTC/USD",is_short=False,exchange="kraken",amount=1,
                        enter_tag="paper:unique-1",orders=[self.order],fee_open=.01,fee_open_currency="USD",fee_close=.01,fee_close_currency="USD")
        self.client=Mock()
        self.client.trades.return_value=[]
        self.client.enter.return_value={"trade_id":10}
        self.client.trade.side_effect=lambda ident:self.trade
        return ident

    def test_partial_duplicate_and_full_fill(self):
        ident=self.prepare()
        self.assertEqual(Decimal(dispatch_intent(self.factory,self.client,ident)["filled_quantity"]),Decimal(1))
        reconcile_intent(self.factory,self.client,ident)
        self.order.update(filled=2,cost=200,status="closed",is_open=False)
        self.assertEqual(reconcile_intent(self.factory,self.client,ident)["status"],"reconciled")
        self.assertEqual(reconcile_intent(self.factory,self.client,ident)["status"],"reconciled")
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount,1).cash,Decimal(798))
            self.assertEqual(db.get(PaperExecutionAccount,1).quantity,Decimal(2))
        self.assertEqual(self.client.enter.call_count,1)

    def test_ambiguous_post_never_retried(self):
        ident=self.prepare()
        self.client.enter.side_effect=TimeoutError()
        with self.assertRaises(PaperExecutionError): dispatch_intent(self.factory,self.client,ident)
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperOrderIntent,ident).status,"unknown")
            self.assertEqual(db.get(PaperOrderIntent,ident).provider_context["tag"],"paper:unique-1")
        with self.assertRaises(PaperExecutionError): dispatch_intent(self.factory,self.client,ident)
        self.assertEqual(self.client.enter.call_count,1)
        self.assertEqual(reconcile_intent(self.factory,self.client,ident,trade_id=10)["status"],"submitted")

    def test_wrong_identity_and_external_order_fail_closed(self):
        ident=self.prepare()
        self.trade["enter_tag"]="foreign"
        with self.assertRaises(PaperExecutionError): dispatch_intent(self.factory,self.client,ident)
        self.trade["enter_tag"]="paper:unique-1"
        self.trade["orders"].append(dict(self.order,order_id="foreign"))
        with self.assertRaises(PaperExecutionError): reconcile_intent(self.factory,self.client,ident,trade_id=10)

    def test_inventory_drift_prevents_post(self):
        ident=self.prepare()
        self.order["is_open"]=False
        self.client.trades.return_value=[self.trade]
        self.assertEqual(dispatch_intent(self.factory,self.client,ident)["reason"],"provider_inventory_drift")
        with Session(self.engine) as db:
            self.assertTrue(db.get(PaperExecutionAccount,1).kill_switch)
        self.client.enter.assert_not_called()

    def test_provider_fee_and_inconsistent_cost_rejected(self):
        ident=self.prepare()
        self.trade["fee_open"]=.02
        with self.assertRaises(PaperExecutionError): dispatch_intent(self.factory,self.client,ident)
        self.trade["fee_open"]=.01
        self.order["cost"]=101
        with self.assertRaises(PaperExecutionError): reconcile_intent(self.factory,self.client,ident,trade_id=10)

    def test_sell_round_trip(self):
        ident=self.prepare()
        self.order.update(filled=2,cost=200,status="closed",is_open=False)
        self.trade["amount"]=2
        dispatch_intent(self.factory,self.client,ident)
        with Session(self.engine) as db, execution_transaction(db):
            db.add(self.decision(2,"sell"))
            db.flush()
            sale=self.reserve(db,decision_id=2,side="sell",client_order_id="sale").id
        self.client.trades.return_value=[self.trade]
        def exit_order(**kwargs):
            self.trade["orders"].append(dict(self.order,order_id="dry2",ft_order_side="sell"))
            return {"result":"Created exit order"}
        self.client.exit.side_effect=exit_order
        self.assertEqual(dispatch_intent(self.factory,self.client,sale)["status"],"reconciled")
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount,1).quantity,0)
            self.assertEqual(db.get(PaperExecutionAccount,1).cash,Decimal(996))

    def test_rejected_provider_order_releases_reserve(self):
        ident=self.prepare()
        self.order.update(filled=0,cost=0,status="rejected",is_open=False)
        self.assertEqual(dispatch_intent(self.factory,self.client,ident)["status"],"rejected")
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount,1).reserved_cash,0)

    def test_unrepresentable_delta_cost_is_blocked(self):
        ident=self.prepare()
        self.order.update(filled=2,cost=199.00000001,safe_price=99.500000005,status="closed",is_open=False)
        with self.assertRaises(PaperExecutionError): dispatch_intent(self.factory,self.client,ident)
        with Session(self.engine) as db:
            self.assertEqual(db.get(PaperExecutionAccount,1).cash,1000)
