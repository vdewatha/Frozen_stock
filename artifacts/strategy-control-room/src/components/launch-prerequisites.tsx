import React from "react";
import type { LaunchPrerequisites } from "@/lib/api";

export function LaunchPrerequisiteResults({ result, error, loading }: {
  result?: LaunchPrerequisites;
  error?: string;
  loading: boolean;
}) {
  return (
    <div className="mb-3 rounded border border-slate-200 bg-white p-3 text-xs" data-testid="launch-prerequisites">
      <div className="flex flex-wrap justify-between gap-2 font-semibold">
        <span>Latest launch prerequisites</span>
        <span className={result?.status === "ready" && !error && !loading ? "text-mint" : "text-amber-800"}>
          {loading ? "Checking — approval unavailable" : error ? "Unknown — refresh failed" : result?.status.replaceAll("_", " ") ?? "Unknown"}
        </span>
      </div>
      {error ? <p role="alert" className="mt-1 text-coral">{error}</p> : null}
      <p className="mt-1">{result?.reason ?? "Prerequisite evidence has not been loaded. Approval is unavailable."}</p>
      {result ? (
        <>
          <p className="mt-1 text-slate-500">Checked {new Date(result.checked_at).toLocaleString()}{loading || error ? " · previous results, not current authorization" : ""}</p>
          <dl className="mt-2 grid gap-2">
            {Object.entries(result.gates).map(([name, gate]) => {
              const outside = gate.status === "unknown" && (gate.evidence as { regular_session?: boolean } | undefined)?.regular_session === false;
              return <div key={name}>
                <dt className={`font-semibold ${gate.status === "pass" ? "text-mint" : gate.status === "fail" || gate.status === "blocked" ? "text-coral" : "text-amber-800"}`}>
                  {name.replaceAll("_", " ")} · {outside ? "unknown outside session" : gate.status === "pass" ? "ready" : gate.status === "fail" ? "blocked" : gate.status}
                </dt>
                {gate.reason ? <dd className="text-slate-600">{gate.reason}</dd> : null}
              </div>;
            })}
          </dl>
        </>
      ) : null}
      <p className="mt-2 text-slate-600">Approval cannot bypass failed or unknown gates, implicitly authorize a provider switch, or grant live trading authority. The required preflight stage must be eligible first.</p>
    </div>
  );
}