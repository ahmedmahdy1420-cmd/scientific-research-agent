import { useCallback, useEffect, useState } from "react";
import { api, RequestError } from "../lib/api";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  NoticeBanner,
  PageHead,
  SkeletonRows,
  Stat,
  formatCost,
  formatMs,
} from "../components/Common";
import {
  IconBolt,
  IconCheck,
  IconGauge,
  IconShield,
  IconX,
} from "../components/Icons";
import type { EvaluationCase, EvaluationResult, SuiteSummary } from "../lib/types";

function rateKind(rate: number): string {
  return rate >= 0.8 ? "ok" : rate >= 0.5 ? "warn" : "bad";
}

export default function Evaluation() {
  const [summary, setSummary] = useState<SuiteSummary[] | null>(null);
  const [results, setResults] = useState<EvaluationResult[]>([]);
  const [cases, setCases] = useState<EvaluationCase[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, r, c] = await Promise.all([
        api.evaluationSummary(),
        api.evaluationResults(),
        api.evaluationCases(),
      ]);
      setSummary(s);
      setResults(r);
      setCases(c);
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "Could not load evaluation data");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      const response = await api.runEvaluation("core", true);
      setMessage(
        `Dispatched suite run ${response.run_label}` +
          (response.task_id ? ` (Celery task ${response.task_id})` : ""),
      );
    } catch (err) {
      setError(
        err instanceof RequestError
          ? `${err.message} (running a suite needs the evaluation:run permission)`
          : "Could not start the suite",
      );
    } finally {
      setRunning(false);
    }
  }

  const bySlug = new Map(cases.map((c) => [c.id, c]));
  const latest = summary?.[0];

  return (
    <>
      <PageHead
        title="Evaluation"
        subtitle="Deterministic checks decide pass/fail; the LLM judge is advisory and recorded alongside so a human can disagree with it. CI gates on the deterministic score alone."
        actions={
          <div className="row">
            <button className="secondary" onClick={() => void load()}>
              Refresh
            </button>
            <button onClick={run} disabled={running}>
              {running ? <span className="spinner on-accent" /> : <IconBolt size={15} />}
              {running ? "Dispatching…" : "Run the core suite"}
            </button>
          </div>
        }
      />

      <ErrorBanner message={error} />
      {message && <NoticeBanner message={message} />}

      <div className="stats summary fade-up" style={{ marginBottom: 16 }}>
        <Stat accent label="Cases defined" value={cases.length} />
        <Stat
          label="Latest pass rate"
          value={latest ? `${(latest.pass_rate * 100).toFixed(0)}%` : "—"}
        />
        <Stat
          label="Latest mean score"
          value={latest ? latest.mean_score.toFixed(3) : "—"}
        />
        <Stat
          accent
          label="Latest cost"
          value={latest ? formatCost(latest.total_cost_usd) : "—"}
        />
      </div>

      {!summary && !error && (
        <Card title="Loading" icon={<IconGauge size={14} />}>
          <SkeletonRows rows={4} />
        </Card>
      )}

      {summary && summary.length === 0 && (
        <Card>
          <EmptyState
            icon={<IconGauge size={20} />}
            title="No suite has been run yet"
            hint="Dispatch the core suite above, or run `make eval` against the stack."
          />
        </Card>
      )}

      {summary && summary.length > 0 && (
        <Card flush title="Suite runs" icon={<IconGauge size={14} />}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Passed</th>
                  <th>Pass rate</th>
                  <th>Mean score</th>
                  <th>Mean latency</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {summary.map((row) => (
                  <tr key={row.run_label}>
                    <td className="mono">{row.run_label}</td>
                    <td className="num">
                      {row.passed}/{row.total}
                    </td>
                    <td style={{ minWidth: 150 }}>
                      <div className="row" style={{ gap: 9, flexWrap: "nowrap" }}>
                        <Badge kind={rateKind(row.pass_rate)}>
                          {(row.pass_rate * 100).toFixed(0)}%
                        </Badge>
                        <div className="meter grow" style={{ minWidth: 48 }}>
                          <span style={{ width: `${row.pass_rate * 100}%` }} />
                        </div>
                      </div>
                    </td>
                    <td className="num">{row.mean_score.toFixed(3)}</td>
                    <td className="num">{formatMs(Math.round(row.mean_latency_ms))}</td>
                    <td className="num">{formatCost(row.total_cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {results.length > 0 && (
        <Card
          title="Latest results"
          icon={<IconShield size={14} />}
          actions={<span className="faint tiny">showing {Math.min(results.length, 25)}</span>}
        >
          {results.slice(0, 25).map((result) => (
            <article
              key={result.id}
              className="evidence"
              style={{
                borderLeftColor: result.passed ? "var(--ok)" : "var(--bad)",
              }}
            >
              <div className="row" style={{ gap: 8 }}>
                <Badge kind={result.passed ? "ok" : "bad"}>
                  {result.passed ? <IconCheck size={10} /> : <IconX size={10} />}
                  {result.passed ? "pass" : "fail"}
                </Badge>
                <span className="mono tiny">
                  {bySlug.get(result.case_id)?.slug ?? result.case_id}
                </span>
                <span className="faint tiny">score {result.overall_score.toFixed(3)}</span>
                <span className="faint tiny">{formatMs(result.latency_ms)}</span>
                <span className="faint tiny">{formatCost(result.cost_usd)}</span>
                {result.human_verdict && (
                  <Badge kind="warn">human: {result.human_verdict}</Badge>
                )}
              </div>

              {result.tools_used.length > 0 && (
                <div className="row" style={{ gap: 5, marginTop: 7 }}>
                  {result.tools_used.map((tool) => (
                    <span key={tool} className="badge plain mono">
                      {tool}
                    </span>
                  ))}
                </div>
              )}

              {result.failures.length > 0 && (
                <ul style={{ color: "var(--bad)", margin: "8px 0 0", paddingLeft: 18 }}>
                  {result.failures.map((failure, i) => (
                    <li key={i}>{failure}</li>
                  ))}
                </ul>
              )}

              {Object.keys(result.judge_scores).length > 0 && (
                <details style={{ marginTop: 4 }}>
                  <summary>judge scores and reasoning (advisory)</summary>
                  <pre>{JSON.stringify(result.judge_scores, null, 2)}</pre>
                  <p className="muted tiny" style={{ marginTop: 6 }}>
                    {result.judge_reasoning}
                  </p>
                </details>
              )}
            </article>
          ))}
        </Card>
      )}
    </>
  );
}
