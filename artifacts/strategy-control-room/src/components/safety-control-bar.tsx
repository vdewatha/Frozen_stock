import { useState, type ReactNode } from "react";
import { AlertTriangle, CirclePause, RotateCcw, ShieldCheck } from "lucide-react";

import { SafetyControlResponse, disableKillSwitch, enableKillSwitch, pauseStrategies, resumeStrategies } from "@/lib/api";
import { RoleGate } from "@/components/access-control";

export type SafetyActionId = "pause" | "resume" | "kill" | "reset";

export type SafetyApi = Record<SafetyActionId, (reason: string) => Promise<SafetyControlResponse>>;

type SafetyActionSpec = {
  label: string;
  requires: "operator" | "admin";
  defaultReason: string;
  className: string;
  icon: ReactNode;
};

export const SAFETY_ACTIONS: Record<SafetyActionId, SafetyActionSpec> = {
  pause: {
    label: "Pause Strategies",
    requires: "operator",
    defaultReason: "Operator paused strategies from the control room.",
    className: "border border-line bg-white font-medium text-ink",
    icon: <CirclePause size={17} />
  },
  resume: {
    label: "Resume",
    requires: "admin",
    defaultReason: "Admin resumed strategies from the control room after review.",
    className: "border border-emerald-200 bg-emerald-50 font-semibold text-mint",
    icon: <ShieldCheck size={17} />
  },
  kill: {
    label: "Kill Switch",
    requires: "operator",
    defaultReason: "Operator engaged the kill switch from the control room.",
    className: "bg-coral font-semibold text-white",
    icon: <AlertTriangle size={17} />
  },
  reset: {
    label: "Reset",
    requires: "admin",
    defaultReason: "Admin reset the kill switch from the control room after review.",
    className: "border border-line bg-white font-medium text-ink",
    icon: <RotateCcw size={17} />
  }
};

export const ALL_SAFETY_ACTIONS: SafetyActionId[] = ["pause", "resume", "kill", "reset"];

export const defaultSafetyApi: SafetyApi = {
  pause: pauseStrategies,
  resume: resumeStrategies,
  kill: enableKillSwitch,
  reset: disableKillSwitch
};

export type SafetyFlowState = { pending: SafetyActionId | null; reason: string };

export type SafetyFlowEvent =
  | { type: "open"; action: SafetyActionId }
  | { type: "edit"; reason: string }
  | { type: "cancel" };

export const idleSafetyFlow: SafetyFlowState = { pending: null, reason: "" };

/** Pure state transitions for the inline reason form (open / edit / cancel). */
export function safetyFlowReducer(state: SafetyFlowState, event: SafetyFlowEvent): SafetyFlowState {
  switch (event.type) {
    case "open":
      return { pending: event.action, reason: SAFETY_ACTIONS[event.action].defaultReason };
    case "edit":
      return state.pending ? { ...state, reason: event.reason } : state;
    case "cancel":
      return idleSafetyFlow;
  }
}

/**
 * Runs the pending action with the entered reason. Returns null (and calls
 * nothing) when no action is pending or the reason is blank.
 */
export async function confirmSafetyAction(
  state: SafetyFlowState,
  api: SafetyApi = defaultSafetyApi
): Promise<SafetyControlResponse | null> {
  const reason = state.reason.trim();
  if (!state.pending || !reason) return null;
  return api[state.pending](reason);
}

export function SafetyControlBar({
  actions = ALL_SAFETY_ACTIONS,
  api = defaultSafetyApi,
  onAction,
  initialFlow = idleSafetyFlow,
  align = "end"
}: {
  actions?: SafetyActionId[];
  api?: SafetyApi;
  onAction?: (response: SafetyControlResponse) => void;
  initialFlow?: SafetyFlowState;
  align?: "start" | "end";
}) {
  const [status, setStatus] = useState("Safety controls ready");
  const [isBusy, setIsBusy] = useState(false);
  const [lastAction, setLastAction] = useState<SafetyControlResponse | null>(null);
  const [flow, setFlow] = useState<SafetyFlowState>(initialFlow);

  function dispatch(event: SafetyFlowEvent) {
    setFlow((current) => safetyFlowReducer(current, event));
  }

  async function confirm() {
    if (!flow.reason.trim()) {
      setStatus("A reason is required for this safety action");
      return;
    }
    setIsBusy(true);
    try {
      const response = await confirmSafetyAction(flow, api);
      if (response) {
        setLastAction(response);
        setStatus(`${response.status}: ${response.message}`);
        onAction?.(response);
      }
      setFlow(idleSafetyFlow);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Safety control failed");
    } finally {
      setIsBusy(false);
    }
  }

  const justify = align === "start" ? "justify-start text-left" : "justify-end text-right";
  const pendingSpec = flow.pending ? SAFETY_ACTIONS[flow.pending] : null;

  return (
    <div className="grid gap-2" data-testid="safety-control-bar">
      <div className={`flex flex-wrap items-center gap-2 ${justify}`}>
        {actions.map((id) => {
          const spec = SAFETY_ACTIONS[id];
          return (
            <RoleGate key={id} requires={spec.requires}>
              <button
                className={`focus-ring inline-flex h-10 items-center gap-2 rounded-md px-3 text-sm disabled:opacity-60 ${spec.className}`}
                data-testid={`safety-action-${id}`}
                disabled={isBusy}
                onClick={() => dispatch({ type: "open", action: id })}
                type="button"
              >
                {spec.icon}
                {spec.label}
              </button>
            </RoleGate>
          );
        })}
      </div>
      {flow.pending && pendingSpec ? (
        <RoleGate requires={pendingSpec.requires}>
          <form
            className={`flex flex-wrap items-end gap-2 rounded-md border border-line bg-panel p-3 text-sm ${justify}`}
            data-testid="safety-reason-form"
            onSubmit={(event) => {
              event.preventDefault();
              void confirm();
            }}
          >
            <label className="grid min-w-[16rem] flex-1 gap-1">
              <span className="text-xs font-medium text-slate-600">Reason for {pendingSpec.label}</span>
              <input
                autoFocus
                className="focus-ring h-9 rounded-md border border-line bg-white px-3"
                data-testid="safety-reason-input"
                onChange={(event) => dispatch({ type: "edit", reason: event.target.value })}
                type="text"
                value={flow.reason}
              />
            </label>
            <button
              className="focus-ring inline-flex h-9 items-center rounded-md bg-ink px-3 text-sm font-semibold text-white disabled:opacity-60"
              data-testid="safety-reason-confirm"
              disabled={isBusy || !flow.reason.trim()}
              type="submit"
            >
              Confirm
            </button>
            <button
              className="focus-ring inline-flex h-9 items-center rounded-md border border-line bg-white px-3 text-sm font-medium"
              data-testid="safety-reason-cancel"
              disabled={isBusy}
              onClick={() => {
                dispatch({ type: "cancel" });
                setStatus("Action cancelled");
              }}
              type="button"
            >
              Cancel
            </button>
          </form>
        </RoleGate>
      ) : null}
      <div className={`text-xs text-slate-500 ${align === "start" ? "text-left" : "text-right"}`} data-testid="safety-status">
        {status}
        {lastAction ? ` | affected ${lastAction.affected_strategy_ids.length}` : ""}
      </div>
    </div>
  );
}
