"use client";

import { useEffect, useState } from "react";
import { LockKeyhole, Send, ShieldAlert } from "lucide-react";

import { BrokerOrderResponse, BrokerStatus, getBrokerStatus, submitBlockedLiveOrder, submitPaperBrokerOrder } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

export function BrokerSafetyLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [quantity, setQuantity] = useState(1);
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [brokerStatus, setBrokerStatus] = useState<BrokerStatus | null>(null);
  const [lastOrder, setLastOrder] = useState<BrokerOrderResponse | null>(null);

  async function refreshStatus() {
    const response = await getBrokerStatus();
    setBrokerStatus(response);
  }

  useEffect(() => {
    let active = true;
    getBrokerStatus().then((response) => {
      if (!active) return;
      setBrokerStatus(response);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Broker status failed"); });
    return () => { active = false; };
  }, []);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshStatus();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Broker action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <LockKeyhole size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Broker Safety Adapter</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_110px_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Qty</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3" min={0.0001} step={0.1} type="number" value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} />
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Submitting paper broker order");
            const response = await submitPaperBrokerOrder(symbol, quantity);
            setLastOrder(response);
            setStatus(`Paper order ${response.status}: ${response.broker_order_id}`);
          })}>
            <Send size={16} />
            Paper Test
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-red-200 bg-red-50 px-3 text-sm font-semibold text-coral" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Testing live-order guard");
            const response = await submitBlockedLiveOrder(symbol, quantity);
            setLastOrder(response);
            setStatus(`Live order ${response.status}: ${response.reason}`);
          })}>
            <ShieldAlert size={16} />
            Live Test
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">Adapter Status</div>
            <StatusPill status={brokerStatus?.live_trading_blocked ? "live_blocked" : "live_enabled"} />
          </div>
          <div className="mt-3 grid gap-2 text-sm">
            <div className="flex justify-between gap-3"><span>Paper broker</span><strong>{brokerStatus?.paper_broker ?? "unknown"}</strong></div>
            <div className="flex justify-between gap-3"><span>Paper enabled</span><strong>{brokerStatus?.paper_trading_enabled ? "Yes" : "No"}</strong></div>
            <div className="flex justify-between gap-3"><span>Live enabled</span><strong>{brokerStatus?.live_trading_enabled ? "Yes" : "No"}</strong></div>
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber">{brokerStatus?.message ?? "Broker boundary not loaded yet"}</div>
          </div>
        </div>

        <div className="rounded-md border border-line p-3">
          <div className="text-sm font-semibold">Last Broker Event</div>
          {lastOrder ? (
            <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
              <span>Status</span><strong className="text-right">{lastOrder.status}</strong>
              <span>Broker</span><strong className="text-right">{lastOrder.broker}</strong>
              <span>Paper only</span><strong className="text-right">{lastOrder.paper_only ? "Yes" : "No"}</strong>
              <span>Order ID</span><strong className="text-right">{lastOrder.broker_order_id ?? "none"}</strong>
              <span>Reason</span><strong className="text-right">{lastOrder.reason ?? "accepted"}</strong>
            </div>
          ) : (
            <div className="mt-3 text-sm text-slate-600">No broker event tested yet</div>
          )}
        </div>
      </div>
    </section>
  );
}
