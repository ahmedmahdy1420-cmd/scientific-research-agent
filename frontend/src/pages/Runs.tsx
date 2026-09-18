import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, RequestError } from "../lib/api";
import { Badge, ErrorBanner, Spinner, formatCost, formatMs, statusKind } from "../components/Common";
import type { AgentRunSummary } from "../lib/types";

export default function Runs() {
  const [runs, setRuns] = useState<AgentRunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listRuns()
      .then((page) => setRuns(page.items))
      .catch((err) =>
        setError(err instanceof RequestError ? err.message : "Could not load runs"),
      );
  }, []);

  return (
    <>
      <h2>Agent Runs</h2>
      <p className="subtitle">
        Every run is persisted with its plan, tool calls, evidence and cost.
      </p>
      <ErrorBanner message={error} />
      {!runs && !error && <Spinner label="Loading runs…" />}
      {runs && runs.length === 0 && <p className="muted">No runs yet. Ask a question first.</p>}
      {runs && runs.length > 0 && (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Question</th><th>Status</th><th>Category</th>
                <th>Tools</th><th>Latency</th><th>Cost</th><th>When</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td><Link to={`/runs/${run.id}`}>{run.question.slice(0, 70)}</Link></td>
                  <td><Badge kind={statusKind(run.status)}>{run.status}</Badge></td>
                  <td className="muted">{run.category ?? "-"}</td>
                  <td>{run.tool_calls_used}</td>
                  <td>{formatMs(run.total_latency_ms)}</td>
                  <td>{formatCost(run.estimated_cost_usd)}</td>
                  <td className="muted">{new Date(run.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
