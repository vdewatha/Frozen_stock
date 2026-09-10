"use client";

import { useEffect, useMemo, useState } from "react";
import { Bell, CheckCheck, ExternalLink, RefreshCw, ShieldAlert } from "lucide-react";

import { NotificationItem, acknowledgeNotification, getNotifications, resolveNotification } from "@/lib/api";

function tone(severity: string): string {
  if (severity === "critical") {
    return "border-coral/30 bg-red-50 text-coral";
  }
  if (severity === "warning") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-emerald-200 bg-emerald-50 text-mint";
}

function categoryLabel(category: string): string {
  if (!category) {
    return "All";
  }
  return category.replaceAll("_", " ");
}

function governanceHref(item: NotificationItem): string {
  if (item.entity_type === "strategy" && typeof item.entity_id === "number") {
    return `#strategy-governance-row-${item.entity_id}`;
  }
  return "#strategy-governance";
}

const categoryFilters = [
  "",
  "strategy_governance",
  "memory_replay",
  "candidate_activation",
  "reactivation_review",
  "risk_alert",
  "decision_journal",
  "scanner_refresh",
  "scheduled_job",
];

export function NotificationCenter() {
  const [statusFilter, setStatusFilter] = useState("open");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [status, setStatus] = useState("Loading notifications");
  const [isBusy, setIsBusy] = useState(true);

  const counts = useMemo(() => {
    return notifications.reduce<Record<string, number>>((memo, item) => {
      memo[item.severity] = (memo[item.severity] ?? 0) + 1;
      return memo;
    }, {});
  }, [notifications]);

  async function refresh(nextStatus = statusFilter, nextCategory = categoryFilter) {
    setIsBusy(true);
    try {
      const rows = await getNotifications(40, nextStatus, nextCategory);
      setNotifications(rows);
      const categoryText = nextCategory ? `${categoryLabel(nextCategory)} ` : "";
      setStatus(`${rows.length} ${nextStatus || "all"} ${categoryText}notifications`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Notification refresh failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getNotifications(40, statusFilter, categoryFilter).then((rows) => {
      if (!active) return;
      setNotifications(rows);
      const categoryText = categoryFilter ? `${categoryLabel(categoryFilter)} ` : "";
      setStatus(`${rows.length} ${statusFilter || "all"} ${categoryText}notifications`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Notification refresh failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, [statusFilter, categoryFilter]);

  async function runAction(action: () => Promise<NotificationItem>) {
    setIsBusy(true);
    try {
      await action();
      await refresh();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Notification action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Bell size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Notification Center</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          {["open", "acknowledged", "resolved", ""].map((value) => (
            <button
              className={statusFilter === value ? "focus-ring h-10 rounded-md bg-mint px-3 text-sm font-semibold text-white" : "focus-ring h-10 rounded-md border border-line px-3 text-sm font-medium"}
              key={value || "all"}
              onClick={() => { if (value !== statusFilter) setIsBusy(true); setStatusFilter(value); }}
              type="button"
            >
              {value || "All"}
            </button>
          ))}
          <select
            aria-label="Notification category"
            className="focus-ring h-10 rounded-md border border-line bg-white px-3 text-sm font-medium"
            disabled={isBusy}
            onChange={(event) => { setIsBusy(true); setCategoryFilter(event.target.value); }}
            value={categoryFilter}
          >
            {categoryFilters.map((value) => (
              <option key={value || "all"} value={value}>{categoryLabel(value)}</option>
            ))}
          </select>
          <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => refresh()} type="button">
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[220px_1fr]">
        <div className="grid content-start gap-2 text-sm">
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="flex items-center gap-2 font-semibold">
              <ShieldAlert size={16} className="text-mint" />
              Severity Mix
            </div>
            <div className="mt-2 grid gap-1 text-slate-600">
              <span>Critical {counts.critical ?? 0}</span>
              <span>Warning {counts.warning ?? 0}</span>
              <span>Info {counts.info ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="max-h-[360px] overflow-auto rounded-md border border-line">
          {notifications.length ? notifications.map((item) => (
            <div className="grid gap-2 border-b border-line p-3 text-sm" key={item.id}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="font-semibold">{item.title}</div>
                  <div className="text-xs text-slate-500">{item.category.replaceAll("_", " ")} | {item.source} | {new Date(item.created_at).toLocaleString()}</div>
                </div>
                <span className={`rounded-md border px-2 py-1 text-xs font-semibold ${tone(item.severity)}`}>{item.severity}</span>
              </div>
              {item.message ? <div className="text-slate-700">{item.message}</div> : null}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs text-slate-500">{item.status}</span>
                <div className="flex flex-wrap gap-2">
                  {item.category === "strategy_governance" ? (
                    <a className="focus-ring inline-flex h-8 items-center gap-2 rounded-md border border-line px-2 text-xs font-medium" href={governanceHref(item)}>
                      <ExternalLink size={14} />
                      Governance
                    </a>
                  ) : null}
                  {item.status === "open" ? (
                    <button className="focus-ring inline-flex h-8 items-center gap-2 rounded-md border border-line px-2 text-xs font-medium" disabled={isBusy} onClick={() => runAction(() => acknowledgeNotification(item.id))} type="button">
                      <CheckCheck size={14} />
                      Acknowledge
                    </button>
                  ) : null}
                  {item.status !== "resolved" ? (
                    <button className="focus-ring inline-flex h-8 items-center gap-2 rounded-md bg-mint px-2 text-xs font-semibold text-white" disabled={isBusy} onClick={() => runAction(() => resolveNotification(item.id))} type="button">
                      Resolve
                    </button>
                  ) : null}
                </div>
              </div>
            </div>
          )) : (
            <div className="p-4 text-sm text-slate-600">No notifications match this filter.</div>
          )}
        </div>
      </div>
    </section>
  );
}
