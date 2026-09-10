"use client";

import { useState } from "react";
import { AlertTriangle, CirclePause, RotateCcw, ShieldCheck } from "lucide-react";

import { SafetyControlResponse, disableKillSwitch, enableKillSwitch, pauseStrategies, resumeStrategies } from "@/lib/api";

export function SafetyControlBar() {
  const [status, setStatus] = useState("Safety controls ready");
  const [isBusy, setIsBusy] = useState(false);
  const [lastAction, setLastAction] = useState<SafetyControlResponse | null>(null);

  async function runAction(action: () => Promise<SafetyControlResponse>) {
    setIsBusy(true);
    try {
      const response = await action();
      setLastAction(response);
      setStatus(`${response.status}: ${response.message}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Safety control failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line bg-white px-3 text-sm font-medium text-ink" disabled={isBusy} onClick={() => runAction(() => pauseStrategies())}>
          <CirclePause size={17} />
          Pause Strategies
        </button>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 text-sm font-semibold text-mint" disabled={isBusy} onClick={() => runAction(() => resumeStrategies())}>
          <ShieldCheck size={17} />
          Resume
        </button>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md bg-coral px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(() => enableKillSwitch())}>
          <AlertTriangle size={17} />
          Kill Switch
        </button>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line bg-white px-3 text-sm font-medium text-ink" disabled={isBusy} onClick={() => runAction(() => disableKillSwitch())}>
          <RotateCcw size={17} />
          Reset
        </button>
      </div>
      <div className="text-right text-xs text-slate-500">
        {status}
        {lastAction ? ` | affected ${lastAction.affected_strategy_ids.length}` : ""}
      </div>
    </div>
  );
}
