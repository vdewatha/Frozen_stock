

import { useEffect, useState } from "react";
import { LockKeyhole } from "lucide-react";

import { BrokerStatus, getBrokerStatus, getErrorMessage } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

export function BrokerSafetyLab() {
  const [status, setStatus] = useState("Ready");
  const [brokerStatus, setBrokerStatus] = useState<BrokerStatus | null>(null);

  useEffect(() => {
    let active = true;
    getBrokerStatus().then((response) => {
      if (!active) return;
      setBrokerStatus(response);
    }).catch(error => { if (active) setStatus(getErrorMessage(error, "Broker status failed")); });
    return () => { active = false; };
  }, []);

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
        <div className="max-w-xl text-sm text-slate-600">
          Manual broker orders and live-order guard tests are intentionally unavailable here. A direct order request returns a deliberate <strong>409 conflict</strong> because this research system is paper-only. Use the risk-gated <strong>Paper Trading Simulator</strong> below to generate paper signals instead.
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
          <div className="mt-3 text-sm text-slate-600">
            No manual broker event is recorded. Paper signals remain subject to readiness and portfolio risk gates.
          </div>
        </div>
      </div>
    </section>
  );
}
