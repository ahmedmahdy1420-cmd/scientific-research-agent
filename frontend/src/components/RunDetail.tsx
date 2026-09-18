/**
 * The run inspector.
 *
 * This is the component that makes the point of the whole project: for any
 * answer you can see the plan, every tool call with its arguments and latency,
 * the evidence that was actually retrieved, the verification verdict, and what
 * it cost.
 */

import type { AgentRun } from "../lib/types";
import { Badge, Stat, formatCost, formatMs, statusKind } from "./Common";

export function RunDetail({
  run,
  onApprove,
  approving,
}: {
  run: AgentRun;
  onApprove?: (approved: boolean, note: string) => void;
  approving?: boolean;
}) {
  const evidenceIds = new Set(run.evidence.map((e) => e.source_id));
  const unresolved = run.citations.filter((c) => !evidenceIds.has(c));

  return (
    <>
      {run.approval && onApprove && (
        <ApprovalGate run={run} onApprove={onApprove} approving={approving ?? false} />
      )}

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            <Badge kind={statusKind(run.status)}>{run.status}</Badge>
            {run.category && <Badge>{run.category}</Badge>}
            {run.confidence && <Badge kind={statusKind(run.confidence)}>
              confidence: {run.confidence}
            </Badge>}
          </div>
          <span className="mono muted">{run.run_id}</span>
        </div>

        {run.answer && (
          <>
            <h3>Answer</h3>
            <div className="answer">{run.answer}</div>
          </>
        )}

        {run.caveats.length > 0 && (
          <>
            <h3>Caveats</h3>
            <ul className="muted">
              {run.caveats.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          </>
        )}

        {run.error && <div className="error" style={{ marginTop: 12 }}>{run.error}</div>}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Cost and latency</h3>
        <div className="stats">
          <Stat label="Total" value={formatMs(run.usage.total_latency_ms)} />
          <Stat label="LLM" value={formatMs(run.usage.llm_latency_ms)} />
          <Stat label="Tools" value={formatMs(run.usage.tool_latency_ms)} />
          <Stat label="Tokens" value={run.usage.total_tokens.toLocaleString()} />
          <Stat label="Cost" value={formatCost(run.usage.estimated_cost_usd)} />
          <Stat label="Tool calls" value={run.usage.tool_calls} />
          <Stat label="Iterations" value={run.usage.iterations} />
          <Stat label="Cache hits" value={run.usage.cache_hits} />
        </div>
        {run.usage.models_used.length > 0 && (
          <p className="muted mono" style={{ marginBottom: 0 }}>
            models: {run.usage.models_used.join(", ")}
          </p>
        )}
      </div>

      {run.plan && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Plan</h3>
          <p className="muted">{run.plan.objective}</p>
          {run.plan.steps.length === 0 ? (
            <p className="muted">No tool calls were planned.</p>
          ) : (
            <ol>
              {run.plan.steps.map((step, i) => (
                <li key={i} style={{ marginBottom: 8 }}>
                  <span className="mono">{step.tool}</span>
                  <div className="muted">{step.rationale}</div>
                  <pre>{JSON.stringify(step.arguments, null, 2)}</pre>
                </li>
              ))}
            </ol>
          )}
          {run.plan.needs_human_approval && (
            <Badge kind="warn">requires human approval</Badge>
          )}
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Tool calls ({run.tool_calls.length})</h3>
        {run.tool_calls.length === 0 ? (
          <p className="muted">No tools were called.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Tool</th><th>Status</th><th>Results</th>
                <th>Latency</th><th>Retries</th><th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {run.tool_calls.map((call) => (
                <tr key={call.id}>
                  <td className="mono">{call.tool_name}</td>
                  <td><Badge kind={statusKind(call.status)}>{call.status}</Badge></td>
                  <td>{call.result_count ?? "-"}</td>
                  <td>{formatMs(call.latency_ms)}</td>
                  <td>{call.retry_count}{call.cache_hit ? " (cached)" : ""}</td>
                  <td className="muted" style={{ maxWidth: 320 }}>
                    {call.denial_reason ?? call.error_code ?? call.result_summary ?? "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {run.verification && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Verification</h3>
          <div className="row">
            <Badge kind={statusKind(run.verification.recommendation)}>
              {run.verification.recommendation}
            </Badge>
            <Badge kind={run.verification.grounded ? "ok" : "bad"}>
              {run.verification.grounded ? "grounded" : "not grounded"}
            </Badge>
            <Badge kind={run.verification.sufficient ? "ok" : "warn"}>
              {run.verification.sufficient ? "sufficient" : "insufficient evidence"}
            </Badge>
          </div>
          <p className="muted">{run.verification.reasoning}</p>
          {run.verification.invalid_citations.length > 0 && (
            <div className="error">
              Citations that did not resolve:{" "}
              <span className="mono">{run.verification.invalid_citations.join(", ")}</span>
            </div>
          )}
          {run.verification.unsupported_claims.length > 0 && (
            <>
              <h3>Unsupported claims</h3>
              <ul>{run.verification.unsupported_claims.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>
          )}
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>
          Evidence ({run.evidence.length}) — cited: {run.citations.length}
        </h3>
        {unresolved.length > 0 && (
          <div className="error">
            Unresolved citations: <span className="mono">{unresolved.join(", ")}</span>
          </div>
        )}
        {run.evidence.length === 0 ? (
          <p className="muted">Nothing was retrieved.</p>
        ) : (
          run.evidence.map((item) => {
            const cited = run.citations.includes(item.source_id);
            return (
              <div
                key={item.source_id}
                style={{
                  borderLeft: `2px solid ${cited ? "var(--ok)" : "var(--border)"}`,
                  padding: "4px 0 10px 12px",
                  marginBottom: 8,
                }}
              >
                <div className="row">
                  <span className="mono">{item.source_id}</span>
                  <Badge>{item.source_type}</Badge>
                  {cited && <Badge kind="ok">cited</Badge>}
                  {item.relevance_score !== null && (
                    <span className="muted mono">score {item.relevance_score.toFixed(3)}</span>
                  )}
                  {item.origin_tool && <span className="muted mono">via {item.origin_tool}</span>}
                </div>
                <strong>{item.title}</strong>
                <div className="muted">{item.snippet}</div>
              </div>
            );
          })
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Graph execution ({run.steps.length} steps)</h3>
        <ul className="timeline">
          {run.steps.map((step, i) => (
            <li key={i}>
              <div className="node">{step.node}</div>
              <div>{step.summary}</div>
              {Object.keys(step.detail).length > 0 && (
                <details>
                  <summary className="muted">detail</summary>
                  <pre>{JSON.stringify(step.detail, null, 2)}</pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}

function ApprovalGate({
  run,
  onApprove,
  approving,
}: {
  run: AgentRun;
  onApprove: (approved: boolean, note: string) => void;
  approving: boolean;
}) {
  let note = "";
  return (
    <div className="notice">
      <strong>Human approval required</strong>
      <p style={{ marginBottom: 8 }}>
        This run has paused. It requested{" "}
        <span className="mono">{run.approval!.action}</span> on{" "}
        <span className="mono">{String(run.approval!.payload.target ?? "an unnamed target")}</span>.
        Nothing has been changed yet.
      </p>
      <p className="muted" style={{ marginTop: 0 }}>
        Risk: {run.approval!.risk_level}. You cannot approve a run you started yourself.
      </p>
      <input
        placeholder="Decision note (optional)"
        style={{ width: "100%", marginBottom: 8 }}
        onChange={(e) => (note = e.target.value)}
      />
      <div className="row">
        <button disabled={approving} onClick={() => onApprove(true, note)}>
          Approve and continue
        </button>
        <button className="danger" disabled={approving} onClick={() => onApprove(false, note)}>
          Reject
        </button>
      </div>
    </div>
  );
}
