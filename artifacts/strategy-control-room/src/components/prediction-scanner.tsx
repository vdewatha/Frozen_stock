

import { useEffect, useState } from "react";
import { BookOpenCheck, BrainCircuit, CheckCircle2, FileSearch, RefreshCw, Target } from "lucide-react";

import { CandidateDecisionJournalEntry, CandidateDecisionScorecard, ScannerRefreshJob, TradeCandidateEvidence, TradeCandidateResponse, createCandidateDecisionJournalEntry, getCandidateDecisionJournal, getCandidateDecisionScorecard, getLatestTradeCandidateRefreshJob, getTradeCandidateEvidence, getTradeCandidates, reviewCandidateActivation, startTradeCandidateRefreshJob } from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function toneFor(status: string): string {
  if (status === "positive_candidate") {
    return "border-emerald-200 bg-emerald-50 text-mint";
  }
  if (status === "needs_more_evidence") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-line bg-panel text-slate-600";
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

export function PredictionScanner() {
  const [snapshot, setSnapshot] = useState<TradeCandidateResponse | null>(null);
  const [status, setStatus] = useState("Loading trade candidates");
  const [isBusy, setIsBusy] = useState(true);
  const [refreshJob, setRefreshJob] = useState<ScannerRefreshJob | null>(null);
  const [evidence, setEvidence] = useState<TradeCandidateEvidence | null>(null);
  const [journalRows, setJournalRows] = useState<CandidateDecisionJournalEntry[]>([]);
  const [decisionScorecard, setDecisionScorecard] = useState<CandidateDecisionScorecard | null>(null);

  async function refresh(force = false) {
    setIsBusy(true);
    try {
      const data = await getTradeCandidates(12, force);
      setSnapshot(data);
      setDecisionScorecard(await getCandidateDecisionScorecard(50));
      setStatus(`${data.positive_count} positive candidates from ${data.candidate_count} scans | ${data.cache_status}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Prediction scan failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function pollRefreshJob(jobId: number) {
    const job = await getLatestTradeCandidateRefreshJob();
    if (!job || job.id !== jobId) {
      return;
    }
    setRefreshJob(job);
    setStatus(job.message ?? `Scanner refresh ${job.status}`);
    if (job.status === "complete") {
      const data = await getTradeCandidates(12, false);
      setSnapshot(data);
      setStatus(`${data.positive_count} positive candidates from ${data.candidate_count} scans | refreshed in background`);
      setIsBusy(false);
      return;
    }
    if (job.status === "failed") {
      setIsBusy(false);
      return;
    }
    window.setTimeout(() => {
      pollRefreshJob(jobId).catch((error) => {
        setStatus(error instanceof Error ? error.message : "Scanner refresh status failed");
        setIsBusy(false);
      });
    }, 3000);
  }

  async function queueRescan() {
    setIsBusy(true);
    try {
      const job = await startTradeCandidateRefreshJob("manual_prediction_scanner", { surface: "prediction_scanner" });
      setRefreshJob(job);
      setStatus(job.message ?? "Scanner refresh queued.");
      pollRefreshJob(job.id);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Scanner refresh queue failed");
      setIsBusy(false);
    }
  }

  async function moveToCandidate(symbol: string, strategy: string) {
    setIsBusy(true);
    try {
      const result = await reviewCandidateActivation(symbol, strategy, "candidate", "Scanner-positive strategy moved into paper candidate review.");
      setStatus(`${result.strategy_name}: ${result.old_status} -> ${result.new_status}`);
      await refresh(false);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Candidate review failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function activateForPaper(symbol: string, strategy: string) {
    setIsBusy(true);
    try {
      const result = await reviewCandidateActivation(symbol, strategy, "activate", "Scanner-positive strategy activated for paper validation.");
      setStatus(result.status === "blocked" ? `${result.strategy_name}: ${result.message}` : `${result.strategy_name}: ${result.old_status} -> ${result.new_status}`);
      await refresh(false);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Paper activation failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function loadEvidence(symbol: string, strategy: string) {
    setIsBusy(true);
    try {
      const data = await getTradeCandidateEvidence(symbol, strategy);
      setEvidence(data);
      setJournalRows(await getCandidateDecisionJournal(5, symbol, strategy));
      setStatus(`Loaded evidence drilldown for ${data.symbol} | ${data.strategy_name}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Evidence drilldown failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function recordJournalDecision(decision: "review" | "skip" | "reject") {
    if (!evidence) {
      return;
    }
    setIsBusy(true);
    try {
      const row = await createCandidateDecisionJournalEntry(
        evidence.symbol,
        evidence.strategy,
        decision,
        "recorded",
        `${decision} recorded from evidence drilldown for ${evidence.symbol} | ${evidence.strategy_name}.`
      );
      setJournalRows([row, ...journalRows].slice(0, 5));
      setDecisionScorecard(await getCandidateDecisionScorecard(50));
      setStatus(`Journaled ${decision} decision for ${evidence.symbol} | ${evidence.strategy_name}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Decision journal write failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getTradeCandidates(12, false).then(async (data) => {
      if (!active) return;
      setSnapshot(data);
      const scorecard = await getCandidateDecisionScorecard(50);
      if (!active) return;
      setDecisionScorecard(scorecard);
      setStatus(`${data.positive_count} positive candidates from ${data.candidate_count} scans | ${data.cache_status}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Prediction scan failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BrainCircuit size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Prediction Scanner</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={queueRescan} type="button">
          <RefreshCw size={16} />
          Rescan
        </button>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[220px_1fr]">
        <div className="grid content-start gap-2 text-sm">
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="flex items-center gap-2 font-semibold">
              <Target size={16} className="text-mint" />
              Scan Result
            </div>
            <div className="mt-2 grid gap-1 text-slate-600">
              <span>Positive {snapshot?.positive_count ?? 0}</span>
              <span>Total {snapshot?.candidate_count ?? 0}</span>
              <span>Status {snapshot?.cache_status ?? "loading"}</span>
              {refreshJob && refreshJob.status !== "complete" ? <span>Refresh {refreshJob.status}</span> : null}
            </div>
          </div>
          {evidence ? (
            <div className="rounded-md border border-line bg-panel p-3">
              <div className="flex items-center gap-2 font-semibold">
                <FileSearch size={16} className="text-mint" />
                Evidence
              </div>
              <div className="mt-2 text-xs font-semibold uppercase text-slate-500">{evidence.symbol} | {evidence.strategy_name}</div>
              <div className="mt-2 grid gap-2 text-xs text-slate-600">
                <div>
                  <div className="font-semibold text-ink">Model Horizons</div>
                  {evidence.model_evidence.predictions.length ? evidence.model_evidence.predictions.map((prediction) => (
                    <div className="mt-1 flex justify-between gap-2" key={prediction.horizon_days}>
                      <span>{prediction.horizon_days}d</span>
                      <span>up {percent.format(prediction.probability_up)} | ER {percent.format(prediction.expected_return)}</span>
                    </div>
                  )) : <div>No model horizons available.</div>}
                </div>
                <div>
                  <div className="font-semibold text-ink">Signal</div>
                  <div>{evidence.signal_evidence.action} | confidence {percent.format(evidence.signal_evidence.confidence)}</div>
                  <div>{evidence.signal_evidence.reason}</div>
                </div>
                <div>
                  <div className="font-semibold text-ink">Backtest</div>
                  <div>score {number.format(evidence.backtest_evidence.score)} | win {percent.format(evidence.backtest_evidence.win_rate)} | trades {evidence.backtest_evidence.number_of_trades}</div>
                  <div>{evidence.backtest_evidence.rejected ? evidence.backtest_evidence.rejection_reasons.join("; ") : "Backtest passed scanner quality gate."}</div>
                </div>
                <div>
                  <div className="font-semibold text-ink">News & Macro</div>
                  <div>{evidence.context_evidence.news.sentiment_label} news | {evidence.context_evidence.market_regime}</div>
                  <div>{evidence.context_evidence.macro.macro_label}</div>
                </div>
                <div>
                  <div className="font-semibold text-ink">Risk Room</div>
                  <div>symbol room {percent.format(evidence.risk_room.symbol_room_pct)} | readiness {evidence.risk_room.readiness_status}</div>
                  <div>{evidence.risk_room.allocation_recommendation}: {evidence.risk_room.allocation_reason}</div>
                </div>
                <div>
                  <div className="font-semibold text-ink">Decision Journal</div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button className="focus-ring inline-flex h-8 items-center gap-1 rounded-md border border-line px-2 text-xs font-semibold" disabled={isBusy} onClick={() => recordJournalDecision("review")} type="button">
                      <BookOpenCheck size={13} />
                      Review
                    </button>
                    <button className="focus-ring inline-flex h-8 items-center gap-1 rounded-md border border-line px-2 text-xs font-semibold" disabled={isBusy} onClick={() => recordJournalDecision("skip")} type="button">
                      <BookOpenCheck size={13} />
                      Skip
                    </button>
                    <button className="focus-ring inline-flex h-8 items-center gap-1 rounded-md border border-line px-2 text-xs font-semibold" disabled={isBusy} onClick={() => recordJournalDecision("reject")} type="button">
                      <BookOpenCheck size={13} />
                      Reject
                    </button>
                  </div>
                  <div className="mt-2 grid gap-1">
                    {journalRows.length ? journalRows.map((row) => (
                      <div className="rounded-md border border-line bg-white p-2" key={row.id}>
                        <div className="font-semibold text-ink">{row.decision} | {row.status}</div>
                        <div>{row.realized_status ?? "not realized"}{row.realized_return !== null ? ` | ${percent.format(row.realized_return)}` : ""}</div>
                      </div>
                    )) : <div>No journal entries for this candidate yet.</div>}
                  </div>
                </div>
              </div>
            </div>
          ) : null}
          {decisionScorecard ? (
            <div className="rounded-md border border-line bg-panel p-3">
              <div className="flex items-center gap-2 font-semibold">
                <BookOpenCheck size={16} className="text-mint" />
                Learning Scorecard
              </div>
              <div className="mt-2 grid gap-1 text-xs text-slate-600">
                <span>Journaled {decisionScorecard.journal_count}</span>
                <span>Scored {decisionScorecard.scored_count}</span>
                <span>Hit rate {decisionScorecard.hit_rate !== null ? percent.format(decisionScorecard.hit_rate) : "pending"}</span>
                <span>Avg return {decisionScorecard.avg_outcome_return !== null ? percent.format(decisionScorecard.avg_outcome_return) : "pending"}</span>
              </div>
              <div className="mt-2 grid gap-1 text-xs text-slate-600">
                {decisionScorecard.rows.slice(0, 3).map((row) => (
                  <div className="rounded-md border border-line bg-white p-2" key={row.id}>
                    <div className="font-semibold text-ink">{row.symbol} | {row.decision}</div>
                    <div>{row.quality}{row.outcome_return !== null ? ` | ${percent.format(row.outcome_return)}` : " | pending"}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        <div className="grid gap-2 lg:grid-cols-2">
          {snapshot?.candidates.length ? snapshot.candidates.map((candidate) => (
            <div className="rounded-md border border-line p-3 text-sm" key={`${candidate.symbol}-${candidate.strategy}`}>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-semibold">{candidate.symbol} | {candidate.strategy_name}</div>
                  <div className="mt-1 text-slate-500">{candidate.market_regime} | {candidate.data_source} | {candidate.strategy_status}</div>
                </div>
                <span className={`rounded-md border px-2 py-1 text-xs font-semibold ${toneFor(candidate.candidate_status)}`}>
                  {candidate.candidate_status.replaceAll("_", " ")}
                </span>
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                <span>Action</span><strong className="text-right">{candidate.action}</strong>
                <span>Probability up</span><strong className="text-right">{percent.format(candidate.probability_up)}</strong>
                <span>Expected return</span><strong className="text-right">{percent.format(candidate.expected_return)}</strong>
                <span>Horizon</span><strong className="text-right">{candidate.horizon_days ? `${candidate.horizon_days}d` : "none"}</strong>
                <span>Backtest score</span><strong className="text-right">{candidate.backtest_score.toFixed(2)}</strong>
                <span>Rank score</span><strong className="text-right">{candidate.score.toFixed(2)}</strong>
                <span>Base score</span><strong className="text-right">{(candidate.base_score ?? candidate.score).toFixed(2)}</strong>
              </div>
              {candidate.journal_feedback?.sample_size ? (
                <div className="mt-3 rounded-md border border-line bg-panel p-2 text-xs text-slate-600">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold uppercase text-slate-500">Journal Memory</span>
                    <strong className={candidate.memory_score_adjustment >= 0 ? "text-mint" : "text-rose-600"}>
                      {signed(candidate.memory_score_adjustment)}
                    </strong>
                  </div>
                  <div className="mt-1">
                    {candidate.journal_feedback.status.replaceAll("_", " ")} from {candidate.journal_feedback.sample_size} scored decision{candidate.journal_feedback.sample_size === 1 ? "" : "s"}
                    {candidate.review_threshold_adjustment ? ` | review threshold ${signed(candidate.review_threshold_adjustment)}` : ""}
                  </div>
                  <div className="mt-1">{candidate.journal_feedback.notes}</div>
                </div>
              ) : null}
              <div className="mt-3 text-slate-700">{candidate.reason}</div>
              <div className="mt-3 rounded-md border border-line bg-panel p-2 text-xs text-slate-600">
                <div className="font-semibold uppercase text-slate-500">Strategy Thesis</div>
                <div className="mt-1 font-medium text-ink">{candidate.strategy_research.source_label}</div>
                <div className="mt-1">{candidate.strategy_research.idea}</div>
                <div className="mt-1">Best fit: {candidate.strategy_research.ideal_market}</div>
                <div className="mt-1">Risk note: {candidate.strategy_research.risk_notes}</div>
              </div>
              {candidate.blockers.length ? (
                <div className="mt-2 grid gap-1 rounded-md border border-line bg-panel p-2 text-xs text-slate-600">
                  {candidate.blockers.map((blocker) => <span key={blocker}>{blocker}</span>)}
                </div>
              ) : (
                <div className="mt-2 rounded-md border border-emerald-200 bg-emerald-50 p-2 text-xs font-semibold text-mint">Model, strategy, and backtest evidence align.</div>
              )}
              {candidate.candidate_status === "positive_candidate" && candidate.strategy_status !== "paper_trading_active" ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-xs font-semibold" disabled={isBusy} onClick={() => loadEvidence(candidate.symbol, candidate.strategy)} type="button">
                    <FileSearch size={15} />
                    Evidence
                  </button>
                  <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-xs font-semibold" disabled={isBusy} onClick={() => moveToCandidate(candidate.symbol, candidate.strategy)} type="button">
                    <CheckCircle2 size={15} />
                    Review for Paper
                  </button>
                  {candidate.strategy_status === "paper_trading_candidate" ? (
                    <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-mint px-3 text-xs font-semibold text-white" disabled={isBusy} onClick={() => activateForPaper(candidate.symbol, candidate.strategy)} type="button">
                      <CheckCircle2 size={15} />
                      Activate Paper
                    </button>
                  ) : null}
                </div>
              ) : null}
              {candidate.candidate_status !== "positive_candidate" || candidate.strategy_status === "paper_trading_active" ? (
                <div className="mt-3">
                  <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-xs font-semibold" disabled={isBusy} onClick={() => loadEvidence(candidate.symbol, candidate.strategy)} type="button">
                    <FileSearch size={15} />
                    Evidence
                  </button>
                </div>
              ) : null}
            </div>
          )) : (
            <div className="rounded-md border border-line p-4 text-sm text-slate-600">No trade candidates loaded.</div>
          )}
        </div>
      </div>
    </section>
  );
}
