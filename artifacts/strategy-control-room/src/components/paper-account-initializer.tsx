import { useState } from "react";
import { Play } from "lucide-react";

export function PaperAccountInitializer({broker, busy, onInitialize}: {
  broker: string; busy: boolean;
  onInitialize: (contract: "legacy-v1" | "alpaca-activities-v2") => void;
}) {
  const [contract, setContract] = useState<"legacy-v1" | "alpaca-activities-v2">("legacy-v1");
  return <div className="flex flex-wrap items-center gap-2">
    {broker === "alpaca_paper" && <label className="text-xs">Accounting contract
      <select aria-label="Accounting contract" className="ml-2 rounded border border-line p-2" disabled={busy}
        value={contract} onChange={event => setContract(event.target.value as typeof contract)}>
        <option value="legacy-v1">Legacy</option>
        <option value="alpaca-activities-v2">Alpaca dated activities v2</option>
      </select>
    </label>}
    <button type="button" data-testid="button-initialize-stock-paper-account" disabled={busy || !["alpaca_paper", "tradier_sandbox"].includes(broker)}
      onClick={() => onInitialize(broker === "alpaca_paper" ? contract : "legacy-v1")}
      className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60">
      <Play size={15}/>Initialize from broker
    </button>
    {broker === "alpaca_paper" && contract === "alpaca-activities-v2" && <p className="w-full text-xs text-slate-600">Imports dated activity accounting. Costs remain unverified; this does not authorize trading.</p>}
  </div>;
}
