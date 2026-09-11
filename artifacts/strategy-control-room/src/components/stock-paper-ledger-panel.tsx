import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Activity, AlertTriangle, CirclePause, Landmark, Play, RefreshCw, ShieldAlert, ShieldCheck, WalletCards } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import { Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Textarea } from "@/components/ui/textarea";
import {
  generateStockPaperSignal,
  getErrorMessage,
  getPaperTrades,
  getStockPaperStatus,
  haltStockPaperAccount,
  initializeStockPaperAccount,
  closeStockPaperPosition,
  dispatchStockPaperOrder,
  reduceStockPaperPosition,
  reconcileStockPaperAccount,
  reserveStockPaperOrder,
  resumeStockPaperAccount,
  type PaperTrade,
  type StockPaperSignalResponse,
  type StockPaperOrder,
  type StockPaperOrderActionResponse,
  type StockPaperStatus,
  type StockPaperStatusValue,
} from "@/lib/api";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
const quantityFormat = new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 4 });

type HaltFormValues = {
  reason: string;
};

type OrderFormValues = {
  symbol: string;
  side: "buy" | "sell";
  quantity: string;
  referencePrice: string;
};

const signalStrategies = [
  ["moving_average_crossover", "Moving Average"],
  ["rsi_mean_reversion", "RSI Reversion"],
  ["macd_momentum", "MACD Momentum"],
  ["ensemble", "Ensemble"],
  ["model_predictive_long", "Model Predictive"],
  ["bollinger_mean_reversion", "Bollinger Reversion"],
  ["channel_breakout", "Channel Breakout"],
  ["trend_pullback", "Trend Pullback"],
];

function numericValue(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value: string | null | undefined): string {
  const parsed = numericValue(value);
  return parsed === null ? "Unavailable" : currency.format(parsed);
}

function formatQuantity(value: string | null | undefined): string {
  const parsed = numericValue(value);
  return parsed === null ? "Unavailable" : quantityFormat.format(parsed);
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "Unavailable";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function unavailableLedger(reason: string): StockPaperStatus {
  return {
    status: "unavailable",
    reason,
    mode: "paper",
    legacy_nonqualifying: true,
    costs_known: false,
    account: null,
    positions: [],
    orders: [],
    fills: [],
    equity_snapshots: [],
  };
}

function statusLabel(status: StockPaperStatusValue | "loading"): string {
  return status === "loading" ? "Loading" : status.replaceAll("_", " ");
}

function statusClasses(status: StockPaperStatusValue | "loading"): string {
  if (status === "reconciled") {
    return "border-emerald-200 bg-emerald-50 text-mint";
  }
  if (status === "halted" || status === "drift" || status === "uncertain" || status === "unavailable") {
    return "border-red-200 bg-red-50 text-coral";
  }
  return "border-amber-200 bg-amber-50 text-amber-700";
}

function profitClasses(value: string | null): string {
  const parsed = numericValue(value);
  if (parsed === null) {
    return "text-slate-500";
  }
  return parsed >= 0 ? "font-semibold text-mint" : "font-semibold text-coral";
}

function legacyProfit(value: string | number | null): string {
  if (value === null) {
    return "Unavailable";
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? currency.format(parsed) : "Unavailable";
}

function newIdempotencyKey(action: string, symbol = "order"): string {
  const randomPart = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `control-room-${action}-${symbol.toLowerCase()}-${randomPart}`;
}

function SignalEvaluationCard({
  signalSymbol,
  signalStrategy,
  evaluatedSignal,
  signalStatus,
  isBusy,
  onSymbolChange,
  onStrategyChange,
  onGenerate,
}: {
  signalSymbol: string;
  signalStrategy: string;
  evaluatedSignal: StockPaperSignalResponse | null;
  signalStatus: string;
  isBusy: boolean;
  onSymbolChange: (symbol: string) => void;
  onStrategyChange: (strategy: string) => void;
  onGenerate: () => void;
}) {
  return (
    <div className="grid gap-3 rounded-md border border-blue-200 bg-blue-50 p-3">
      <div>
        <div className="text-sm font-semibold text-blue-950">Generate and persist a signal for BUY</div>
        <div className="mt-1 text-xs leading-5 text-blue-900">
          Raw BUY reservations are rejected. This persists a broker-bound signal but does not create an order or prove
          strategy evidence; reservation performs the final risk/evidence checks.
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-[1fr_1.5fr_auto]">
        <label className="grid gap-1 text-xs font-semibold text-blue-950">
          Symbol
          <input
            className="focus-ring h-9 rounded-md border border-blue-200 bg-white px-3 text-sm uppercase"
            data-testid="input-stock-paper-signal-symbol"
            value={signalSymbol}
            onChange={(event) => onSymbolChange(event.target.value.toUpperCase())}
            placeholder="SPY"
          />
        </label>
        <label className="grid gap-1 text-xs font-semibold text-blue-950">
          Strategy
          <select
            className="focus-ring h-9 rounded-md border border-blue-200 bg-white px-3 text-sm"
            data-testid="select-stock-paper-signal-strategy"
            value={signalStrategy}
            onChange={(event) => onStrategyChange(event.target.value)}
          >
            {signalStrategies.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <button
          className="focus-ring inline-flex h-9 items-center justify-center gap-2 self-end rounded-md border border-blue-300 bg-white px-3 text-xs font-semibold text-blue-900 disabled:opacity-60"
          data-testid="button-generate-stock-paper-signal"
          disabled={isBusy}
          onClick={onGenerate}
          type="button"
        >
          <RefreshCw size={14} />
          Generate / evaluate
        </button>
      </div>
      <div className="text-xs text-blue-900" data-testid="text-stock-paper-signal-status">{signalStatus}</div>
      {evaluatedSignal ? (
        <div className="grid gap-1 rounded-md border border-blue-200 bg-white p-2 text-xs text-slate-700 sm:grid-cols-2">
          <span>Signal action: <strong>{evaluatedSignal.signal_action}</strong></span>
          <span>Signal ID: <strong>{evaluatedSignal.signal_id}</strong></span>
          <span>Confidence: <strong>{percent.format(Number(evaluatedSignal.confidence))}</strong></span>
          <span>Reference price: <strong>{formatMoney(evaluatedSignal.reference_price)}</strong></span>
          <span>Execution created: <strong>{evaluatedSignal.execution_created ? "Yes" : "No — reserve separately"}</strong></span>
          <span className="sm:col-span-2">Evidence: <strong>Not returned; BUY reservation verifies approved non-legacy evidence and hard-blocks when missing.</strong></span>
        </div>
      ) : null}
    </div>
  );
}

export function StockPaperLedgerPanel() {
  const [ledger, setLedger] = useState<StockPaperStatus | null>(null);
  const [legacyTrades, setLegacyTrades] = useState<PaperTrade[]>([]);
  const [legacyError, setLegacyError] = useState("");
  const [statusMessage, setStatusMessage] = useState("Loading broker-reported Alpaca paper ledger");
  const [isBusy, setIsBusy] = useState(false);
  const [pendingOrder, setPendingOrder] = useState<StockPaperOrder | null>(null);
  const [lastOrderAction, setLastOrderAction] = useState<StockPaperOrderActionResponse | null>(null);
  const [signalSymbol, setSignalSymbol] = useState("SPY");
  const [signalStrategy, setSignalStrategy] = useState("moving_average_crossover");
  const [evaluatedSignal, setEvaluatedSignal] = useState<StockPaperSignalResponse | null>(null);
  const [signalStatus, setSignalStatus] = useState("No stored signal has been generated.");
  const [manualOrderKey, setManualOrderKey] = useState(() => newIdempotencyKey("reserve"));
  const positionActionKeys = useRef(new Map<string, string>());
  const positionActionByOrder = useRef(new Map<number, string>());
  const haltForm = useForm<HaltFormValues>({ defaultValues: { reason: "" } });
  const orderForm = useForm<OrderFormValues>({
    defaultValues: { symbol: "", side: "buy", quantity: "", referencePrice: "" },
  });

  const refresh = useCallback(async () => {
    setIsBusy(true);
    try {
      const nextLedger = await getStockPaperStatus();
      setLedger(nextLedger);
      setStatusMessage(`Updated ${formatTimestamp(nextLedger.account?.source_timestamp ?? null)}`);
      try {
        setLegacyTrades(await getPaperTrades());
        setLegacyError("");
      } catch (error) {
        setLegacyError(getErrorMessage(error, "Legacy simulator records are unavailable."));
      }
    } catch (error) {
      const message = getErrorMessage(error, "Stock paper ledger is unavailable.");
      setLedger((current) => current ?? unavailableLedger(message));
      setStatusMessage(message);
    } finally {
      setIsBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => {
      void refresh();
    }, 30_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function runAction(
    action: () => Promise<StockPaperStatus>,
    pendingMessage: string,
  ): Promise<boolean> {
    setIsBusy(true);
    setStatusMessage(pendingMessage);
    try {
      const nextLedger = await action();
      setLedger(nextLedger);
      setStatusMessage(`${statusLabel(nextLedger.status)}: ${nextLedger.reason}`);
      return true;
    } catch (error) {
      setStatusMessage(getErrorMessage(error, "Stock paper ledger action failed."));
      return false;
    } finally {
      setIsBusy(false);
    }
  }

  async function submitHalt({ reason }: HaltFormValues) {
    const trimmedReason = reason.trim();
    const completed = await runAction(
      () => haltStockPaperAccount(trimmedReason),
      "Halting new Alpaca paper exposure",
    );
    if (completed) {
      haltForm.reset();
    }
  }

  async function refreshStatusOnly() {
    try {
      setLedger(await getStockPaperStatus());
    } catch (error) {
      setStatusMessage(getErrorMessage(error, "The ledger action completed, but status refresh failed."));
    }
  }

  async function submitManualOrder({ symbol, side, quantity, referencePrice }: OrderFormValues) {
    const normalizedSymbol = symbol.trim().toUpperCase();
    const signalId = evaluatedSignal?.signal_id ?? null;
    if (side === "buy" && (
      !signalId
      || evaluatedSignal?.signal_action.toUpperCase() !== "BUY"
      || evaluatedSignal?.symbol.toUpperCase() !== normalizedSymbol
      || evaluatedSignal?.strategy !== signalStrategy
    )) {
      setStatusMessage("Raw BUY reservations are disabled. Generate and evaluate a BUY signal here, then reserve it with the returned signal ID.");
      return;
    }
    setIsBusy(true);
    setStatusMessage(`Creating a safe ${side} reservation for ${normalizedSymbol}`);
    try {
      const response = await reserveStockPaperOrder({
        symbol: normalizedSymbol,
        side,
        quantity: quantity.trim(),
        reference_price: referencePrice.trim(),
        idempotency_key: manualOrderKey,
        ...(side === "buy" && signalId ? { signal_id: signalId } : {}),
        source: "manual_control_room",
      });
      setLastOrderAction(response);
      setPendingOrder(response.order);
      setStatusMessage(`Order ${response.order.id} is reserved. Dispatch it once to Alpaca paper.`);
      orderForm.reset();
      setManualOrderKey(newIdempotencyKey("reserve"));
      await refreshStatusOnly();
    } catch (error) {
      setStatusMessage(getErrorMessage(error, "Safe paper order reservation failed."));
    } finally {
      setIsBusy(false);
    }
  }

  async function generateAndEvaluateSignal() {
    const normalizedSymbol = signalSymbol.trim().toUpperCase();
    if (!normalizedSymbol) {
      setSignalStatus("Enter a symbol before generating a signal.");
      return;
    }
    setIsBusy(true);
    setSignalStatus(`Generating and evaluating ${signalStrategy} for ${normalizedSymbol}`);
    try {
      const response = await generateStockPaperSignal(normalizedSymbol, signalStrategy);
      setEvaluatedSignal(response);
      orderForm.setValue("symbol", normalizedSymbol);
      orderForm.setValue("referencePrice", response.reference_price);
      if (response.signal_action.toUpperCase() === "BUY") {
        orderForm.setValue("side", "buy");
      } else {
        orderForm.setValue("side", "sell");
      }
      setSignalStatus(
        `Stored signal ${response.signal_id} generated (${response.signal_action}). Approved non-legacy evidence is not returned here; BUY reservation will hard-block if evidence is missing.`,
      );
      setStatusMessage(`Stored signal ${response.signal_id} generated: ${response.signal_action}. Execution was not created.`);
    } catch (error) {
      setEvaluatedSignal(null);
      setSignalStatus(getErrorMessage(error, "Signal generation/evaluation failed."));
      setStatusMessage(getErrorMessage(error, "Signal generation/evaluation failed."));
    } finally {
      setIsBusy(false);
    }
  }

  function updateSignalInputs(updates: { symbol?: string; strategy?: string }) {
    if (updates.symbol !== undefined) {
      setSignalSymbol(updates.symbol);
    }
    if (updates.strategy !== undefined) {
      setSignalStrategy(updates.strategy);
    }
    setEvaluatedSignal(null);
    setSignalStatus("Signal inputs changed; generate/evaluate again before reserving BUY.");
  }

  async function dispatchPendingOrder() {
    const order = pendingOrder;
    if (!order || order.uncertain_submission || order.status === "unknown") {
      setStatusMessage("Dispatch is unavailable. Reconcile the Alpaca paper account before taking another action.");
      return;
    }
    if (status !== "reconciled") {
      setStatusMessage("Dispatch is blocked until the Alpaca paper account is reconciled.");
      return;
    }
    setIsBusy(true);
    setStatusMessage(`Dispatching reserved order ${order.id} once to Alpaca paper`);
    try {
      const response = await dispatchStockPaperOrder(order.id);
      setLastOrderAction(response);
      setPendingOrder(response.order.uncertain_submission || response.order.status === "unknown" ? response.order : null);
      const positionActionKey = positionActionByOrder.current.get(order.id);
      if (positionActionKey) {
        positionActionKeys.current.delete(positionActionKey);
        positionActionByOrder.current.delete(order.id);
      }
      setStatusMessage(
        response.action === "halted_uncertain" || response.order.uncertain_submission || response.order.status === "unknown"
          ? "Dispatch outcome is uncertain. Reconcile before any further action; retry is intentionally unavailable."
          : `Order ${response.order.id} was submitted to Alpaca paper.`,
      );
      await refreshStatusOnly();
    } catch (error) {
      setPendingOrder({ ...order, status: "unknown", uncertain_submission: true });
      setStatusMessage("Dispatch outcome is uncertain. Reconcile before any further action; retry is intentionally unavailable.");
      if (error instanceof Error && error.message) {
        setStatusMessage(`${error.message} Dispatch outcome is uncertain; reconcile before any further action.`);
      }
    } finally {
      setIsBusy(false);
    }
  }

  async function executePositionAction(symbol: string, action: "close" | "reduce") {
    setIsBusy(true);
    setStatusMessage(`${action === "close" ? "Reserving full close" : "Reserving 25% reduction"} for ${symbol}`);
    const keyName = `${action}:${symbol}`;
    const idempotencyKey = positionActionKeys.current.get(keyName) ?? newIdempotencyKey(action, symbol);
    positionActionKeys.current.set(keyName, idempotencyKey);
    try {
      const response = action === "close"
        ? await closeStockPaperPosition(symbol, idempotencyKey)
        : await reduceStockPaperPosition(symbol, "0.25", idempotencyKey);
      setLastOrderAction(response);
      setPendingOrder(response.order);
      positionActionByOrder.current.set(response.order.id, keyName);
      setStatusMessage(`Safe ${action} reservation ${response.order.id} created. Review it, then dispatch once.`);
      await refreshStatusOnly();
    } catch (error) {
      setStatusMessage(getErrorMessage(error, `Safe ${action} reservation failed.`));
    } finally {
      setIsBusy(false);
    }
  }

  const status = ledger?.status ?? "loading";
  const account = ledger?.account ?? null;
  const positions = ledger?.positions ?? [];
  const orders = ledger?.orders ?? [];
  const fills = ledger?.fills ?? [];
  const snapshots = ledger?.equity_snapshots ?? [];
  const unknownCostFills = useMemo(() => fills.filter((fill) => !fill.cost_known).length, [fills]);
  const statusNeedsAttention = status !== "reconciled";
  const canReconcile = Boolean(account) && status !== "unavailable" && status !== "loading";
  const canHalt = Boolean(account) && status !== "halted" && status !== "unavailable" && status !== "loading";
  const canResume = Boolean(account) && (status === "halted" || status === "drift" || status === "uncertain");
  const canCreateSafeOrders = status === "reconciled" && Boolean(account) && !pendingOrder;

  return (
    <section className="rounded-md border border-line bg-white" data-testid="panel-stock-paper-ledger">
      <div className="flex flex-col gap-3 border-b border-line p-4 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Landmark size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Alpaca Paper Ledger</h2>
            <span className={`inline-flex items-center rounded border px-2 py-1 text-xs font-semibold capitalize ${statusClasses(status)}`} data-testid="status-stock-paper-ledger">
              {statusLabel(status)}
            </span>
            <span className="inline-flex items-center rounded border border-slate-200 bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">
              Paper only
            </span>
          </div>
          <p className="mt-1 max-w-3xl text-sm text-slate-500">
            Broker-reported Alpaca sandbox activity. This ledger imports and reconciles the existing account; it never resets,
            funds, or submits unreserved/arbitrary orders.
          </p>
          <p className="mt-1 text-xs text-slate-500" data-testid="text-stock-paper-ledger-status">
            {statusMessage}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium disabled:opacity-60"
            data-testid="button-refresh-stock-paper-ledger"
            disabled={isBusy}
            onClick={() => void refresh()}
            type="button"
          >
            <RefreshCw size={15} />
            Refresh
          </button>
          {status === "uninitialized" ? (
            <RoleGate requires="admin">
              <button
                className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60"
                data-testid="button-initialize-stock-paper-account"
                disabled={isBusy}
                onClick={() => void runAction(initializeStockPaperAccount, "Importing existing Alpaca paper account; no reset will occur")}
                type="button"
              >
                <Play size={15} />
                Initialize from broker
              </button>
            </RoleGate>
          ) : null}
          {canReconcile ? (
            <RoleGate requires="operator">
              <button
                className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium disabled:opacity-60"
                data-testid="button-reconcile-stock-paper-account"
                disabled={isBusy}
                onClick={() => void runAction(reconcileStockPaperAccount, "Reconciling Alpaca account, positions, orders, and fills")}
                type="button"
              >
                <RefreshCw size={15} />
                Reconcile
              </button>
            </RoleGate>
          ) : null}
          {canResume ? (
            <RoleGate requires="admin">
              <button
                className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 text-sm font-semibold text-mint disabled:opacity-60"
                data-testid="button-resume-stock-paper-account"
                disabled={isBusy}
                onClick={() => void runAction(resumeStockPaperAccount, "Checking post-halt reconciliation before resume")}
                type="button"
              >
                <ShieldCheck size={15} />
                Resume
              </button>
            </RoleGate>
          ) : null}
        </div>
      </div>

      {ledger ? (
        <div className="grid gap-3 border-b border-line p-4">
          <div className={`flex items-start gap-2 rounded-md border p-3 text-sm ${statusNeedsAttention ? "border-amber-200 bg-amber-50 text-amber-800" : "border-emerald-200 bg-emerald-50 text-mint"}`} data-testid="alert-stock-paper-ledger-state">
            {statusNeedsAttention ? <AlertTriangle size={17} className="mt-0.5 shrink-0" /> : <ShieldCheck size={17} className="mt-0.5 shrink-0" />}
            <div>
              <div className="font-semibold capitalize">{statusLabel(ledger.status)} account state</div>
              <div className="mt-1">{ledger.reason}</div>
              {ledger.status !== "reconciled" ? (
                <div className="mt-1 font-medium">Account value and P/L are unavailable to qualifying dashboard metrics until reconciliation succeeds.</div>
              ) : !ledger.costs_known ? (
                <div className="mt-1 font-medium">Broker P/L is intentionally withheld until complete cash flows and reported costs are verified.</div>
              ) : null}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="inline-flex items-center gap-1 rounded border border-slate-200 bg-slate-100 px-2 py-1 font-semibold text-slate-600">
              <ShieldAlert size={13} />
              Legacy simulator is nonqualifying
            </span>
            <span className={`inline-flex items-center gap-1 rounded border px-2 py-1 font-semibold ${ledger.costs_known ? "border-emerald-200 bg-emerald-50 text-mint" : "border-amber-200 bg-amber-50 text-amber-800"}`} data-testid="status-stock-paper-costs">
              {ledger.costs_known ? "Reported costs complete" : "Reported costs incomplete — unknown"}
            </span>
          </div>
        </div>
      ) : null}

      <div className="grid gap-3 border-b border-line p-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-md border border-line bg-panel p-3" data-testid="metric-stock-paper-cash">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase text-slate-500"><WalletCards size={14} /> Reported cash</div>
          <div className="mt-2 text-xl font-semibold">{formatMoney(account?.cash)}</div>
          <div className="mt-1 text-xs text-slate-500">Last broker snapshot: {formatTimestamp(account?.source_timestamp)}</div>
        </div>
        <div className="rounded-md border border-line bg-panel p-3" data-testid="metric-stock-paper-equity">
          <div className="text-xs font-semibold uppercase text-slate-500">Reported equity</div>
          <div className="mt-2 text-xl font-semibold">{formatMoney(account?.equity)}</div>
          <div className="mt-1 text-xs text-slate-500">Last broker snapshot: {formatTimestamp(account?.source_timestamp)}</div>
        </div>
        <div className="rounded-md border border-line bg-panel p-3" data-testid="metric-stock-paper-buying-power">
          <div className="text-xs font-semibold uppercase text-slate-500">Buying power</div>
          <div className="mt-2 text-xl font-semibold">{formatMoney(account?.buying_power)}</div>
          <div className="mt-1 text-xs text-slate-500">Observed from Alpaca paper account</div>
        </div>
        <div className="rounded-md border border-line bg-panel p-3" data-testid="metric-stock-paper-last-equity">
          <div className="text-xs font-semibold uppercase text-slate-500">Last equity</div>
          <div className="mt-2 text-xl font-semibold">{formatMoney(account?.last_equity)}</div>
          <div className="mt-1 text-xs text-slate-500">Reconciled: {formatTimestamp(account?.last_reconciled_at)}</div>
        </div>
      </div>

      <div className="grid gap-3 border-b border-line p-4 md:grid-cols-3">
        <div className="rounded-md border border-line p-3 text-sm">
          <div className="flex items-center gap-2 font-semibold"><Activity size={15} className="text-mint" /> Account identity</div>
          <div className="mt-2 grid gap-1 text-xs text-slate-600">
            <span>Venue: <strong className="text-slate-800">Alpaca paper</strong></span>
            <span>Account: <strong className="break-all text-slate-800">{account?.account_id ?? "Unavailable"}</strong></span>
            <span>Currency: <strong className="text-slate-800">{account?.currency ?? "USD"}</strong></span>
          </div>
        </div>
        <div className="rounded-md border border-line p-3 text-sm">
          <div className="flex items-center gap-2 font-semibold"><ShieldCheck size={15} className="text-mint" /> Ledger timestamps</div>
          <div className="mt-2 grid gap-1 text-xs text-slate-600">
            <span>Initialized: <strong className="text-slate-800">{formatTimestamp(account?.initialized_at)}</strong></span>
            <span>Source: <strong className="text-slate-800">{formatTimestamp(account?.source_timestamp)}</strong></span>
            <span>Reconciled: <strong className="text-slate-800">{formatTimestamp(account?.last_reconciled_at)}</strong></span>
          </div>
        </div>
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900" data-testid="card-stock-paper-cost-assumptions">
          <div className="flex items-center gap-2 font-semibold"><ShieldAlert size={15} /> Cost accounting boundary</div>
          <div className="mt-2 text-xs leading-5">
            Broker-reported fees are shown per fill when present. Commission, spread, and slippage completeness is unknown;
            no modeled costs are added to P/L.
          </div>
        </div>
      </div>

      {canHalt ? (
        <RoleGate requires="operator" className="border-b border-line p-4">
          <Form {...haltForm}>
            <form className="grid gap-3 rounded-md border border-red-200 bg-red-50 p-3" onSubmit={haltForm.handleSubmit(submitHalt)}>
              <div className="flex items-start gap-2">
                <CirclePause size={17} className="mt-0.5 shrink-0 text-coral" />
                <div>
                  <div className="text-sm font-semibold text-coral">Halt new exposure</div>
                  <div className="text-xs text-red-800">Use for an operator safety stop. Resume requires admin approval after successful reconciliation.</div>
                </div>
              </div>
              <FormField
                control={haltForm.control}
                name="reason"
                rules={{ required: "Enter a halt reason for the audit trail." }}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-xs font-semibold text-red-900">Reason</FormLabel>
                    <FormControl>
                      <Textarea
                        {...field}
                        className="min-h-16 bg-white text-sm"
                        data-testid="input-stock-paper-halt-reason"
                        placeholder="Explain why new stock paper exposure should stop"
                      />
                    </FormControl>
                    <FormDescription className="text-xs text-red-800">This reason is persisted with the broker ledger event.</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <div>
                <button
                  className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-coral px-3 text-sm font-semibold text-white disabled:opacity-60"
                  data-testid="button-halt-stock-paper-account"
                  disabled={isBusy}
                  type="submit"
                >
                  <CirclePause size={15} />
                  Halt account
                </button>
              </div>
            </form>
          </Form>
        </RoleGate>
      ) : null}

      {!canCreateSafeOrders ? (
        <RoleGate requires="operator" className="border-b border-line p-4">
          <SignalEvaluationCard
            evaluatedSignal={evaluatedSignal}
            isBusy={isBusy}
            onGenerate={() => void generateAndEvaluateSignal()}
            onStrategyChange={(strategy) => updateSignalInputs({ strategy })}
            onSymbolChange={(symbol) => updateSignalInputs({ symbol })}
            signalStatus={signalStatus}
            signalStrategy={signalStrategy}
            signalSymbol={signalSymbol}
          />
        </RoleGate>
      ) : null}

      {canCreateSafeOrders ? (
        <RoleGate requires="operator" className="border-b border-line p-4">
          <Form {...orderForm}>
            <form className="grid gap-3 rounded-md border border-emerald-200 bg-emerald-50 p-3" onSubmit={orderForm.handleSubmit(submitManualOrder)}>
              <div className="flex items-start gap-2">
                <ShieldCheck size={17} className="mt-0.5 shrink-0 text-mint" />
                <div>
                  <div className="text-sm font-semibold text-mint">Safe paper order reservation</div>
                  <div className="text-xs text-emerald-900">
                    This creates a durable DAY limit reservation at the reference price, then requires one explicit dispatch.
                    Broker, session, cash, freshness, and risk gates remain server-enforced.
                  </div>
                </div>
              </div>
              <SignalEvaluationCard
                evaluatedSignal={evaluatedSignal}
                isBusy={isBusy}
                onGenerate={() => void generateAndEvaluateSignal()}
                onStrategyChange={(strategy) => updateSignalInputs({ strategy })}
                onSymbolChange={(symbol) => updateSignalInputs({ symbol })}
                signalStatus={signalStatus}
                signalStrategy={signalStrategy}
                signalSymbol={signalSymbol}
              />
              <div className="grid gap-3 md:grid-cols-4">
                <FormField
                  control={orderForm.control}
                  name="symbol"
                  rules={{ required: "Enter a stock symbol." }}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-xs font-semibold text-emerald-950">Symbol</FormLabel>
                      <FormControl>
                        <input
                          {...field}
                          className="focus-ring h-9 w-full rounded-md border border-emerald-200 bg-white px-3 text-sm uppercase"
                          data-testid="input-stock-paper-order-symbol"
                          placeholder="SPY"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={orderForm.control}
                  name="side"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-xs font-semibold text-emerald-950">Side</FormLabel>
                      <FormControl>
                        <select
                          {...field}
                          className="focus-ring h-9 w-full rounded-md border border-emerald-200 bg-white px-3 text-sm"
                          data-testid="select-stock-paper-order-side"
                        >
                          <option value="buy" disabled={!evaluatedSignal?.signal_id || evaluatedSignal.signal_action.toUpperCase() !== "BUY"}>Buy (stored signal)</option>
                          <option value="sell">Sell</option>
                        </select>
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={orderForm.control}
                  name="quantity"
                  rules={{ required: "Enter a positive quantity." }}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-xs font-semibold text-emerald-950">Quantity</FormLabel>
                      <FormControl>
                        <input
                          {...field}
                          className="focus-ring h-9 w-full rounded-md border border-emerald-200 bg-white px-3 text-sm"
                          data-testid="input-stock-paper-order-quantity"
                          inputMode="decimal"
                          min="0"
                          placeholder="1.00000000"
                          step="any"
                          type="number"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={orderForm.control}
                  name="referencePrice"
                  rules={{ required: "Enter a positive reference price." }}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-xs font-semibold text-emerald-950">Reference limit price</FormLabel>
                      <FormControl>
                        <input
                          {...field}
                          className="focus-ring h-9 w-full rounded-md border border-emerald-200 bg-white px-3 text-sm"
                          data-testid="input-stock-paper-order-reference-price"
                          inputMode="decimal"
                          min="0"
                          placeholder="500.00000000"
                          step="any"
                          type="number"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-emerald-900">
                  Source is fixed to <strong>manual_control_room</strong>. BUY requires the evaluated signal ID above;
                  SELL remains position-limited. No market order or live route is available.
                </p>
                <button
                  className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60"
                  data-testid="button-reserve-stock-paper-order"
                  disabled={isBusy}
                  type="submit"
                >
                  <Play size={15} />
                  Reserve safe order
                </button>
              </div>
            </form>
          </Form>
        </RoleGate>
      ) : null}

      {pendingOrder ? (
        <div className={`border-b p-4 ${pendingOrder.uncertain_submission || pendingOrder.status === "unknown" ? "border-red-200 bg-red-50" : "border-emerald-200 bg-emerald-50"}`} data-testid="card-stock-paper-pending-order">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className={`text-sm font-semibold ${pendingOrder.uncertain_submission || pendingOrder.status === "unknown" || status !== "reconciled" ? "text-coral" : "text-mint"}`}>
                {pendingOrder.uncertain_submission || pendingOrder.status === "unknown"
                  ? "Dispatch outcome uncertain"
                  : status !== "reconciled"
                    ? "Reserved order blocked until reconciliation"
                    : "Reserved order awaiting dispatch"}
              </div>
              <div className="mt-1 text-xs text-slate-700">
                Order {pendingOrder.id}: {pendingOrder.side.toUpperCase()} {formatQuantity(pendingOrder.quantity)} {pendingOrder.symbol}
                {pendingOrder.limit_price ? ` at ${formatMoney(pendingOrder.limit_price)}` : ""}
              </div>
              <div className="mt-1 text-xs text-slate-600">
                Client key: <span className="break-all">{pendingOrder.client_order_id}</span>
              </div>
              {pendingOrder.side.toLowerCase() === "buy" ? (
                <div className={`mt-1 text-xs font-medium ${pendingOrder.evidence_id ? "text-emerald-800" : "text-coral"}`}>
                  Signal {pendingOrder.signal_id ?? "not reported"} · Evidence {pendingOrder.evidence_id ?? "missing — BUY must remain blocked"}
                </div>
              ) : null}
            </div>
            {pendingOrder.uncertain_submission || pendingOrder.status === "unknown" ? (
              <div className="max-w-sm text-right text-xs font-medium text-coral">
                Reconcile the broker account before any further action. This panel intentionally does not offer a retry.
              </div>
            ) : status !== "reconciled" ? (
              <div className="max-w-sm text-right text-xs font-medium text-coral">
                Reconcile and resume the account before dispatching this existing reservation.
              </div>
            ) : (
              <RoleGate requires="operator">
                <button
                  className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60"
                  data-testid="button-dispatch-stock-paper-order"
                  disabled={isBusy}
                  onClick={() => void dispatchPendingOrder()}
                  type="button"
                >
                  <Play size={15} />
                  Dispatch once
                </button>
              </RoleGate>
            )}
          </div>
        </div>
      ) : lastOrderAction ? (
        <div className="border-b border-line bg-panel px-4 py-3 text-xs text-slate-700" data-testid="text-stock-paper-last-order-action">
          Last safe order action: <strong>{lastOrderAction.action.replaceAll("_", " ")}</strong> · order {lastOrderAction.order.id} · status {lastOrderAction.order.status}
        </div>
      ) : null}

      <div className="border-b border-line">
        <div className="flex items-center justify-between gap-3 border-b border-line p-3">
          <div className="flex items-center gap-2 text-sm font-semibold"><Landmark size={16} className="text-mint" /> Current positions</div>
          <span className="text-xs text-slate-500" data-testid="text-stock-paper-position-count">{positions.length} reported</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] border-collapse text-sm">
            <thead className="bg-panel text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2">Symbol</th>
                <th className="px-3 py-2 text-right">Quantity</th>
                <th className="px-3 py-2 text-right">Avg entry</th>
                <th className="px-3 py-2 text-right">Current</th>
                <th className="px-3 py-2 text-right">Market value</th>
                <th className="px-3 py-2 text-right">Cost basis</th>
                <th className="px-3 py-2 text-right">Unrealized P/L</th>
                <th className="px-3 py-2">Observed</th>
                <th className="px-3 py-2 text-right">Safe action</th>
              </tr>
            </thead>
            <tbody>
              {positions.length ? positions.map((position) => (
                <tr className="border-t border-line" data-testid={`row-stock-paper-position-${position.symbol}`} key={position.symbol}>
                  <td className="px-3 py-2 font-semibold">{position.symbol}</td>
                  <td className="px-3 py-2 text-right">{formatQuantity(position.quantity)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(position.average_entry_price)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(position.current_price)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(position.market_value)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(position.cost_basis)}</td>
                  <td className={`px-3 py-2 text-right ${profitClasses(position.unrealized_pl)}`}>{formatMoney(position.unrealized_pl)}</td>
                  <td className="px-3 py-2 text-xs text-slate-500">{formatTimestamp(position.observed_at)}</td>
                  <td className="px-3 py-2">
                    <RoleGate requires="operator" className="flex justify-end gap-2">
                      <button
                        className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md border border-line bg-white px-2 text-xs font-medium disabled:opacity-60"
                        data-testid={`button-reduce-stock-paper-position-${position.symbol}`}
                        disabled={isBusy || status !== "reconciled" || Boolean(pendingOrder)}
                        onClick={() => void executePositionAction(position.symbol, "reduce")}
                        title="Reserve and dispatch a safe 25% reduction"
                        type="button"
                      >
                        <RefreshCw size={13} />
                        Reduce 25%
                      </button>
                      <button
                        className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md bg-coral px-2 text-xs font-semibold text-white disabled:opacity-60"
                        data-testid={`button-close-stock-paper-position-${position.symbol}`}
                        disabled={isBusy || status !== "reconciled" || Boolean(pendingOrder)}
                        onClick={() => void executePositionAction(position.symbol, "close")}
                        title="Reserve and dispatch a safe full close"
                        type="button"
                      >
                        <CirclePause size={13} />
                        Close
                      </button>
                    </RoleGate>
                  </td>
                </tr>
              )) : (
                <tr className="border-t border-line"><td className="px-3 py-3 text-slate-600" colSpan={9}>{account ? "No reported long positions." : "Positions unavailable until the broker account is initialized."}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid gap-4 border-b border-line p-4 xl:grid-cols-2">
        <div className="rounded-md border border-line">
          <div className="flex items-center justify-between gap-3 border-b border-line p-3">
            <div className="text-sm font-semibold">Broker-reported orders</div>
            <span className="text-xs text-slate-500" data-testid="text-stock-paper-order-count">{orders.length} retained</span>
          </div>
          <div className="max-h-80 overflow-auto">
            {orders.length ? orders.map((order) => (
              <div className="grid gap-2 border-b border-line p-3 text-xs last:border-b-0" data-testid={`row-stock-paper-order-${order.client_order_id}`} key={order.client_order_id}>
                <div className="flex items-start justify-between gap-2">
                  <div className="font-semibold">{order.symbol} · {order.side.toUpperCase()} {formatQuantity(order.quantity)}</div>
                  <span className={`rounded border px-2 py-0.5 font-semibold ${order.uncertain_submission ? "border-red-200 bg-red-50 text-coral" : "border-slate-200 bg-slate-100 text-slate-600"}`}>{order.uncertain_submission ? "uncertain" : order.status}</span>
                </div>
                <div className="grid gap-1 text-slate-500">
                  <span>Client ID: <strong className="break-all text-slate-700">{order.client_order_id}</strong></span>
                  <span>Broker ID: <strong className="break-all text-slate-700">{order.broker_order_id ?? "Not reported"}</strong></span>
                  <span>Reserved cash: <strong className="text-slate-700">{formatMoney(order.reserved_cash)}</strong> · Submitted {formatTimestamp(order.submitted_at)}</span>
                </div>
              </div>
            )) : <div className="p-3 text-sm text-slate-600">No broker orders have been imported.</div>}
          </div>
        </div>

        <div className="rounded-md border border-line">
          <div className="flex items-center justify-between gap-3 border-b border-line p-3">
            <div className="text-sm font-semibold">Broker-reported fills</div>
            <span className="text-xs text-amber-700" data-testid="text-stock-paper-fill-cost-count">{unknownCostFills} unknown cost record{unknownCostFills === 1 ? "" : "s"}</span>
          </div>
          <div className="max-h-80 overflow-auto">
            {fills.length ? fills.map((fill) => (
              <div className="grid gap-2 border-b border-line p-3 text-xs last:border-b-0" data-testid={`row-stock-paper-fill-${fill.broker_activity_id}`} key={fill.broker_activity_id}>
                <div className="flex items-start justify-between gap-2">
                  <div className="font-semibold">{fill.symbol} · {fill.side.toUpperCase()} {formatQuantity(fill.quantity)} @ {formatMoney(fill.price)}</div>
                  <span className={`rounded border px-2 py-0.5 font-semibold ${fill.cost_known ? "border-emerald-200 bg-emerald-50 text-mint" : "border-amber-200 bg-amber-50 text-amber-800"}`}>{fill.cost_known ? "cost known" : "cost unknown"}</span>
                </div>
                <div className="grid gap-1 text-slate-500">
                  <span>Reported fee: <strong className="text-slate-700">{fill.fee === null ? "Not reported" : formatMoney(fill.fee)}</strong></span>
                  <span>Activity: <strong className="break-all text-slate-700">{fill.broker_activity_id}</strong></span>
                  <span>Filled: <strong className="text-slate-700">{formatTimestamp(fill.filled_at)}</strong></span>
                </div>
              </div>
            )) : <div className="p-3 text-sm text-slate-600">No broker fills have been imported.</div>}
          </div>
        </div>
      </div>

      <div className="border-b border-line p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold"><Activity size={16} className="text-mint" /> Equity snapshots</div>
          <span className="text-xs text-slate-500">{snapshots.length} retained</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-sm">
            <thead className="bg-panel text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2">Observed</th>
                <th className="px-3 py-2 text-right">Cash</th>
                <th className="px-3 py-2 text-right">Equity</th>
                <th className="px-3 py-2 text-right">Last equity</th>
                <th className="px-3 py-2 text-right">Buying power</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.length ? snapshots.map((snapshot) => (
                <tr className="border-t border-line" data-testid={`row-stock-paper-equity-${snapshot.observed_at}`} key={snapshot.observed_at}>
                  <td className="px-3 py-2 text-xs text-slate-500">{formatTimestamp(snapshot.observed_at)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(snapshot.cash)}</td>
                  <td className="px-3 py-2 text-right font-semibold">{formatMoney(snapshot.equity)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(snapshot.last_equity)}</td>
                  <td className="px-3 py-2 text-right">{formatMoney(snapshot.buying_power)}</td>
                </tr>
              )) : (
                <tr className="border-t border-line"><td className="px-3 py-3 text-slate-600" colSpan={5}>Equity snapshots are unavailable until explicit initialization.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="border-t border-slate-200 bg-slate-50 p-4" data-testid="section-legacy-stock-paper-records">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-slate-700">Legacy local simulator records</h3>
          <span className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-semibold uppercase text-slate-500">Nonqualifying evidence</span>
        </div>
        <p className="mt-1 max-w-4xl text-xs leading-5 text-slate-600">
          These historical rows come from the old local paper simulator, not Alpaca-reported fills. They remain separate and
          must not be used as stock paper account value, equity, P/L, or execution evidence.
        </p>
        {legacyError ? <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800" data-testid="alert-legacy-stock-paper-error">{legacyError}</div> : null}
        {legacyTrades.length ? (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-sm">
              <thead className="text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2">Symbol</th>
                  <th className="px-3 py-2">Side/status</th>
                  <th className="px-3 py-2">Entry</th>
                  <th className="px-3 py-2">Exit</th>
                  <th className="px-3 py-2 text-right">P/L</th>
                </tr>
              </thead>
              <tbody>
                {legacyTrades.slice(0, 8).map((trade) => (
                  <tr className="border-t border-slate-200" data-testid={`row-legacy-stock-paper-trade-${trade.id}`} key={trade.id}>
                    <td className="px-3 py-2 font-semibold">{trade.symbol}</td>
                    <td className="px-3 py-2 capitalize">{trade.side} · {trade.status ?? "unknown"}</td>
                    <td className="px-3 py-2">{formatMoney(trade.entry_price === null ? null : String(trade.entry_price))} · {formatTimestamp(trade.entry_time)}</td>
                    <td className="px-3 py-2">{formatMoney(trade.exit_price === null ? null : String(trade.exit_price))} · {formatTimestamp(trade.exit_time)}</td>
                    <td className={`px-3 py-2 text-right ${trade.profit_loss === null ? "text-slate-500" : Number(trade.profit_loss) >= 0 ? "font-semibold text-mint" : "font-semibold text-coral"}`}>{legacyProfit(trade.profit_loss)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : !legacyError ? <div className="mt-3 text-sm text-slate-500">No legacy simulator rows were returned.</div> : null}
      </div>
    </section>
  );
}