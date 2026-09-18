import { useCallback, useEffect, useState } from "react";
import { api, RequestError } from "../lib/api";
import { Badge, ErrorBanner, Spinner, formatCost, formatMs } from "../components/Common";
import type { EvaluationCase, EvaluationResult, SuiteSummary } from "../lib/types";

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

  return (
    <>
      <h2>Evaluation</h2>
      <p className="subtitle">
        Deterministic checks decide pass/fail; the LLM judge is advisory and recorded
        alongside so a human can disagree with it.
      </p>

      <ErrorBanner message={error} />
      {message && <div className="notice">{message}</div>}

      <div className="card">
        <div className="row">
          <button onClick={run} disabled={running}>
            {running ? "Dispatching…" : "Run the core suite"}
          </button>
          <button className="secondary" onClick={() => void load()}>Refresh</button>
          <span className="muted">{cases.length} cases defined</span>
        </div>
      </div>

      {!summary && !error && <Spinner label="Loading evaluation history…" />}

      {summary && summary.length > 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Suite runs</h3>
          <table>
            <thead>
              <tr>
                <th>Run</th><th>Passed</th><th>Pass rate</th>
                <th>Mean score</th><th>Mean latency</th><th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {summary.map((row) => (
                <tr key={row.run_label}>
                  <td className="mono">{row.run_label}</td>
                  <td>{row.passed}/{row.total}</td>
                  <td>
                    <Badge kind={row.pass_rate >= 0.8 ? "ok" : row.pass_rate >= 0.5 ? "warn" : "bad"}>
                      {(row.pass_rate * 100).toFixed(0)}%
                    </Badge>
                  </td>
                  <td>{row.mean_score.toFixed(3)}</td>
                  <td>{formatMs(Math.round(row.mean_latency_ms))}</td>
                  <td>{formatCost(row.total_cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {results.length > 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Latest results</h3>
          {results.slice(0, 25).map((result) => (
            <div
              key={result.id}
              style={{
                borderLeft: `2px solid ${result.passed ? "var(--ok)" : "var(--bad)"}`,
                padding: "4px 0 10px 12px",
                marginBottom: 10,
              }}
            >
              <div className="row">
                <Badge kind={result.passed ? "ok" : "bad"}>
                  {result.passed ? "pass" : "fail"}
                </Badge>
                <span className="mono">{bySlug.get(result.case_id)?.slug ?? result.case_id}</span>
                <span className="muted">score {result.overall_score.toFixed(3)}</span>
                <span className="muted">{formatMs(result.latency_ms)}</span>
                <span className="muted">{formatCost(result.cost_usd)}</span>
                {result.human_verdict && (
                  <Badge kind="warn">human: {result.human_verdict}</Badge>
                )}
              </div>
              {result.tools_used.length > 0 && (
                <div className="muted mono">tools: {result.tools_used.join(", ")}</div>
              )}
              {result.failures.length > 0 && (
                <ul style={{ color: "var(--bad)", margin: "6px 0" }}>
                  {result.failures.map((failure, i) => <li key={i}>{failure}</li>)}
                </ul>
              )}
              {Object.keys(result.judge_scores).length > 0 && (
                <details>
                  <summary className="muted">judge scores and reasoning</summary>
                  <pre>{JSON.stringify(result.judge_scores, null, 2)}</pre>
                  <p className="muted">{result.judge_reasoning}</p>
                </details>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
