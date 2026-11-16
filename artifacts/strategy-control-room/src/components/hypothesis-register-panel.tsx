import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ClipboardList } from "lucide-react";
import { API_BASE_URL, authenticatedFetch, getErrorMessage, readJson } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

export type HypothesisStatus = "passed" | "failed" | "inconclusive" | "collecting";
type RowAbility = { strategy_type?: string | null; status: string | null; can_place: boolean; detail: string };

export type HypothesisEntry = {
  id: string;
  source: "report" | "forward_experiment" | "strategy_discovery" | "experiment_engine";
  family: string;
  title: string;
  hypothesis: string | null;
  data: string;
  status: HypothesisStatus;
  result: string;
  why: string;
  caveat?: string;
  decided_on: string | null;
  next_decision: { date: string | null; note?: string } | null;
  sample?: { required: number | null; observed: number | null; expected_trades_per_year: number | null; years_to_verdict: number | null };
  revisit_if: string;
  sources: { reports: string[]; notes: string | null; pr: number | null };
  can_place_paper_orders: boolean;
  paper_orders: { can_place: boolean; detail: string; rows: RowAbility[] };
};

export type HypothesisRegister = {
  as_of: string;
  updated: string | null;
  statuses: HypothesisStatus[];
  counts: Record<HypothesisStatus, number>;
  can_place_paper_orders: number;
  closed_families: Record<string, string[]>;
  entries: HypothesisEntry[];
  unavailable: { source: string; error: string }[];
  note: string;
};

async function loadRegister(): Promise<HypothesisRegister> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/hypothesis-register`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Hypothesis register unavailable (${response.status})`);
  return readJson(response);
}

const STATUS_LABEL: Record<HypothesisStatus, string> = {
  passed: "Passed", failed: "Failed", inconclusive: "Inconclusive", collecting: "Collecting evidence",
};
const SOURCE_LABEL: Record<HypothesisEntry["source"], string> = {
  report: "Study", forward_experiment: "Forward experiment", strategy_discovery: "Discovery", experiment_engine: "Learning engine",
};

function label(value: string) { return value.replaceAll("_", " "); }

function nextDecision(entry: HypothesisEntry) {
  const parts: string[] = [];
  if (entry.next_decision?.date) parts.push(entry.next_decision.date);
  if (entry.next_decision?.note) parts.push(entry.next_decision.note);
  if (entry.sample?.required) {
    parts.push(`needs ${entry.sample.required} trades${entry.sample.observed === null ? "" : `, has ${entry.sample.observed}`}`);
    if (entry.sample.years_to_verdict) parts.push(`about ${entry.sample.years_to_verdict} years at the expected rate`);
  }
  return parts.length ? parts.join(" · ") : "None scheduled";
}

export function HypothesisRegisterView({ register, filter = "all", onFilter }: {
  register: HypothesisRegister; filter?: HypothesisStatus | "all"; onFilter?: (value: HypothesisStatus | "all") => void;
}) {
  const shown = register.entries.filter(entry => filter === "all" || entry.status === filter);
  const closed = Object.keys(register.closed_families);
  return <div className="short-monitor space-y-8">
    <section>
      <div className="section-heading"><h2><ClipboardList size={17} className="mr-2 inline" />What the research has established</h2>
        <span className="section-meta">{register.entries.length} hypotheses · register updated {register.updated ?? "n/a"}</span></div>
      <p className="section-meta">{register.note}</p>
      <dl className="short-monitor-metrics">
        {register.statuses.map(status => <div key={status}><dt>{STATUS_LABEL[status]}</dt><dd>{register.counts[status] ?? 0}</dd></div>)}
        <div><dt>Can place paper orders</dt><dd>{register.can_place_paper_orders}<small>of {register.entries.length}</small></dd></div>
      </dl>
      {register.unavailable.map(item => <p key={item.source} role="alert" className="workspace-error">{label(item.source)} unavailable: {item.error}</p>)}
      {closed.length > 0 && <p className="section-meta">Closed families (every study failed; a new proposal must name its new evidence): {closed.map(label).join(", ")}.</p>}
    </section>

    <section>
      <div className="section-heading"><h2>Hypotheses</h2>
        <span className="section-meta">{filter === "all" ? "All" : STATUS_LABEL[filter]} · {shown.length} shown</span></div>
      {onFilter && <div className="page-tabs" role="group" aria-label="Filter by verdict">
        {(["all", ...register.statuses] as const).map(value => <button key={value} type="button" aria-pressed={filter === value}
          onClick={() => onFilter(value)}>{value === "all" ? "All" : STATUS_LABEL[value]}</button>)}
      </div>}
      {shown.length ? <div className="short-monitor-table"><table>
        <thead><tr><th>Hypothesis and data</th><th>Verdict and reason</th><th>Next decision</th><th>Paper orders</th></tr></thead>
        <tbody>{shown.map(entry => <tr key={entry.id}>
          <th scope="row">{entry.title}<small className="block font-normal">{entry.hypothesis ?? ""}</small>
            <small className="block font-normal">Tested on: {entry.data}</small>
            <small className="block font-normal">{SOURCE_LABEL[entry.source]} · {label(entry.family)} · {entry.sources.reports.join(", ")}{entry.sources.pr !== null ? ` · PR #${entry.sources.pr}` : ""}</small></th>
          <td><StatusPill status={entry.status} />{entry.decided_on && <small>decided {entry.decided_on}</small>}
            <span className="block">{entry.result}</span>
            <small className="block">Why: {entry.why}</small>
            {entry.caveat && <small className="block">Caveat: {entry.caveat}</small>}</td>
          <td>{nextDecision(entry)}<small className="block">Revisit if: {entry.revisit_if}</small></td>
          <td>{entry.can_place_paper_orders ? "Yes" : "No"}<small className="block">{entry.paper_orders.detail}</small></td>
        </tr>)}</tbody></table></div>
        : <p className="section-meta">No hypothesis has this verdict.</p>}
    </section>
  </div>;
}

export function HypothesisRegisterPanel() {
  const [filter, setFilter] = useState<HypothesisStatus | "all">("all");
  const query = useQuery({ queryKey: ["hypothesis-register"], queryFn: loadRegister, refetchInterval: 300_000, retry: 1 });
  if (query.error) return <p role="alert" className="workspace-error">{getErrorMessage(query.error, "Hypothesis register unavailable")}</p>;
  if (!query.data) return <p role="status">Loading hypothesis register...</p>;
  return <HypothesisRegisterView register={query.data} filter={filter} onFilter={setFilter} />;
}
