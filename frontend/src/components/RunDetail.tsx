/**
 * The run inspector.
 *
 * This is the component that makes the point of the whole project: for any
 * answer you can see the plan, every tool call with its arguments and latency,
 * the evidence that was actually retrieved, the verification verdict, and what
 * it cost. The styling exists to keep that density readable — nothing here is
 * summarised away.
 */

import { useState } from "react";
import type { AgentRun, EvidenceItem } from "../lib/types";
import { Badge, Card, Stat, formatCost, formatMs, statusKind } from "./Common";
import {
  IconAlert,
  IconBolt,
  IconCheck,
  IconClock,
  IconDocument,
  IconGraph,
  IconQuote,
  IconShield,
  IconSpark,
  IconWand,
  IconX,
} from "./Icons";

const CONFIDENCE_FILL: Record<string, number> = { high: 1, medium: 0.62, low: 0.3 };

const EVIDENCE_ICON = {
  document: IconDocument,
  experiment: IconWand,
  clinical_trial: IconBolt,
  compound: IconSpark,
} satisfies Record<EvidenceItem["source_type"], typeof IconDocument>;

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
    <div className="fade-up">
      {run.approval && onApprove && (
        <ApprovalGate run={run} onApprove={onApprove} approving={approving ?? false} />
      )}

      {/* --- the answer ---------------------------------------------------- */}
      <section className="card accent">
        <div className="row between" style={{ marginBottom: 14 }}>
          <div className="row" style={{ gap: 7 }}>
            <Badge kind={statusKind(run.status)}>{run.status.replace(/_/g, " ")}</Badge>
            {run.category && <Badge kind="info">{run.category}</Badge>}
            {run.verification && (
              <Badge kind={run.verification.grounded ? "ok" : "bad"}>
                {run.verification.grounded ? "grounded" : "not grounded"}
              </Badge>
            )}
          </div>
          <span className="mono faint tiny" title="Agent run id">
            {run.run_id}
          </span>
        </div>

        {run.confidence && (
          <div style={{ maxWidth: 260, marginBottom: 16 }}>
            <div className="row between tiny muted" style={{ marginBottom: 5 }}>
              <span className="eyebrow">confidence</span>
              <span className="mono">{run.confidence}</span>
            </div>
            <div className="meter">
              <span style={{ width: `${(CONFIDENCE_FILL[run.confidence] ?? 0.5) * 100}%` }} />
            </div>
          </div>
        )}

        {run.answer ? (
          <div className="answer answer-block">{run.answer}</div>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            No answer was produced for this run.
          </p>
        )}

        {run.caveats.length > 0 && (
          <>
            <h3 className="section">Caveats</h3>
            <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
              {run.caveats.map((caveat, i) => (
                <li key={i}>{caveat}</li>
              ))}
            </ul>
          </>
        )}

        {run.citations.length > 0 && (
          <div className="row" style={{ gap: 6, marginTop: 16 }}>
            <IconQuote size={14} className="faint" />
            {run.citations.map((citation) => (
              <span key={citation} className="badge ok mono">
                {citation}
              </span>
            ))}
          </div>
        )}

        {run.error && (
          <div className="banner error" style={{ marginTop: 14, marginBottom: 0 }}>
            <IconAlert size={16} />
            <span>{run.error}</span>
          </div>
        )}
      </section>

      {/* --- cost and latency ---------------------------------------------- */}
      <Card title="Cost and latency" icon={<IconClock size={14} />}>
        <div className="stats">
          <Stat accent label="Total" value={formatMs(run.usage.total_latency_ms)} />
          <Stat label="LLM" value={formatMs(run.usage.llm_latency_ms)} />
          <Stat label="Tools" value={formatMs(run.usage.tool_latency_ms)} />
          <Stat label="Retrieval" value={formatMs(run.usage.retrieval_latency_ms)} />
          <Stat label="Tokens" value={run.usage.total_tokens.toLocaleString()} />
          <Stat accent label="Cost" value={formatCost(run.usage.estimated_cost_usd)} />
          <Stat label="Tool calls" value={run.usage.tool_calls} />
          <Stat label="Iterations" value={run.usage.iterations} />
          <Stat label="Cache hits" value={run.usage.cache_hits} />
        </div>
        {run.usage.models_used.length > 0 && (
          <div className="row" style={{ gap: 6, marginTop: 12 }}>
            <span className="eyebrow">models</span>
            {run.usage.models_used.map((model) => (
              <span key={model} className="badge plain mono">
                {model}
              </span>
            ))}
          </div>
        )}
      </Card>

      {/* --- the plan ------------------------------------------------------ */}
      {run.plan && (
        <Card
          title="Plan"
          icon={<IconWand size={14} />}
          actions={
            run.plan.needs_human_approval ? (
              <Badge kind="warn">requires human approval</Badge>
            ) : undefined
          }
        >
          <p className="muted" style={{ marginTop: 0 }}>
            {run.plan.objective}
          </p>
          {run.plan.steps.length === 0 ? (
            <p className="faint" style={{ marginBottom: 0 }}>
              No tool calls were planned.
            </p>
          ) : (
            <ol className="timeline" style={{ counterReset: "step" }}>
              {run.plan.steps.map((step, i) => (
                <li key={i}>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="node">{step.tool}</span>
                    <span className="faint tiny">step {i + 1}</span>
                  </div>
                  <div className="muted">{step.rationale}</div>
                  <details>
                    <summary>arguments</summary>
                    <pre>{JSON.stringify(step.arguments, null, 2)}</pre>
                  </details>
                </li>
              ))}
            </ol>
          )}
        </Card>
      )}

      {/* --- tool calls ---------------------------------------------------- */}
      <Card
        flush
        title={`Tool calls (${run.tool_calls.length})`}
        icon={<IconBolt size={14} />}
        actions={
          <span className="faint tiny">
            authorised in application code, before the tool runs
          </span>
        }
      >
        {run.tool_calls.length === 0 ? (
          <p className="faint" style={{ padding: 18, margin: 0 }}>
            No tools were called.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Status</th>
                  <th>Results</th>
                  <th>Latency</th>
                  <th>Retries</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {run.tool_calls.map((call) => (
                  <tr key={call.id}>
                    <td className="mono">{call.tool_name}</td>
                    <td>
                      <Badge kind={statusKind(call.status)}>{call.status}</Badge>
                    </td>
                    <td className="num">{call.result_count ?? "—"}</td>
                    <td className="num">{formatMs(call.latency_ms)}</td>
                    <td className="num">
                      {call.retry_count}
                      {call.cache_hit && (
                        <span className="badge info" style={{ marginLeft: 6 }}>
                          cached
                        </span>
                      )}
                    </td>
                    <td className="muted" style={{ maxWidth: 320 }}>
                      {call.denial_reason ?? call.error_code ?? call.result_summary ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* --- verification --------------------------------------------------- */}
      {run.verification && (
        <Card title="Verification" icon={<IconShield size={14} />}>
          <div className="row" style={{ gap: 7 }}>
            <Badge kind={statusKind(run.verification.recommendation)}>
              {run.verification.recommendation.replace(/_/g, " ")}
            </Badge>
            <Badge kind={run.verification.grounded ? "ok" : "bad"}>
              {run.verification.grounded ? "grounded" : "not grounded"}
            </Badge>
            <Badge kind={run.verification.sufficient ? "ok" : "warn"}>
              {run.verification.sufficient ? "sufficient" : "insufficient evidence"}
            </Badge>
          </div>
          <p className="muted" style={{ marginTop: 12 }}>
            {run.verification.reasoning}
          </p>
          {run.verification.invalid_citations.length > 0 && (
            <div className="banner error">
              <IconAlert size={16} />
              <span>
                Citations that did not resolve:{" "}
                <span className="mono">{run.verification.invalid_citations.join(", ")}</span>
              </span>
            </div>
          )}
          {run.verification.unsupported_claims.length > 0 && (
            <>
              <h3 className="section">Unsupported claims</h3>
              <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
                {run.verification.unsupported_claims.map((claim, i) => (
                  <li key={i}>{claim}</li>
                ))}
              </ul>
            </>
          )}
        </Card>
      )}

      {/* --- evidence ------------------------------------------------------- */}
      <Card
        title={`Evidence (${run.evidence.length})`}
        icon={<IconQuote size={14} />}
        actions={
          <span className="faint tiny">
            {run.citations.length} cited · {run.evidence.length - run.citations.length} retrieved
            but unused
          </span>
        }
      >
        {unresolved.length > 0 && (
          <div className="banner error">
            <IconAlert size={16} />
            <span>
              Unresolved citations: <span className="mono">{unresolved.join(", ")}</span>
            </span>
          </div>
        )}
        {run.evidence.length === 0 ? (
          <p className="faint" style={{ marginBottom: 0 }}>
            Nothing was retrieved.
          </p>
        ) : (
          run.evidence.map((item) => {
            const cited = run.citations.includes(item.source_id);
            const Icon = EVIDENCE_ICON[item.source_type] ?? IconDocument;
            return (
              <article key={item.source_id} className={`evidence${cited ? " cited" : ""}`}>
                <div className="row" style={{ gap: 7 }}>
                  <Icon size={13} className="faint" />
                  <span className="mono tiny">{item.source_id}</span>
                  <Badge plain>{item.source_type.replace(/_/g, " ")}</Badge>
                  {cited && (
                    <Badge kind="ok">
                      <IconCheck size={10} />
                      cited
                    </Badge>
                  )}
                  {item.relevance_score !== null && (
                    <span className="faint mono tiny">
                      score {item.relevance_score.toFixed(3)}
                    </span>
                  )}
                  {item.origin_tool && (
                    <span className="faint mono tiny">via {item.origin_tool}</span>
                  )}
                </div>
                <div className="title">{item.title}</div>
                <div className="snippet">{item.snippet}</div>
              </article>
            );
          })
        )}
      </Card>

      {/* --- graph trace ---------------------------------------------------- */}
      <Card title={`Graph execution (${run.steps.length} steps)`} icon={<IconGraph size={14} />}>
        <ul className="timeline">
          {run.steps.map((step, i) => (
            <li key={i}>
              <div className="node">{step.node}</div>
              <div>{step.summary}</div>
              {Object.keys(step.detail).length > 0 && (
                <details>
                  <summary>detail</summary>
                  <pre>{JSON.stringify(step.detail, null, 2)}</pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </div>
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
  const [note, setNote] = useState("");
  const approval = run.approval!;

  return (
    <div className="approval">
      <div className="row between" style={{ marginBottom: 10 }}>
        <div className="row" style={{ gap: 9 }}>
          <IconShield size={17} />
          <strong style={{ color: "var(--text-strong)" }}>Human approval required</strong>
        </div>
        <Badge kind="warn">risk: {approval.risk_level}</Badge>
      </div>

      <p style={{ color: "var(--text)", marginBottom: 10 }}>
        This run has paused at the approval gate. It requested{" "}
        <span className="mono">{approval.action}</span> on{" "}
        <span className="mono">{String(approval.payload.target ?? "an unnamed target")}</span>.
        Nothing has been changed yet — the graph state is checkpointed in Postgres until someone
        decides.
      </p>

      <p className="muted tiny" style={{ marginBottom: 12 }}>
        {approval.reason} You cannot approve a run you started yourself, and approving does not
        grant a permission you do not already hold.
      </p>

      <input
        placeholder="Decision note (optional)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        style={{ width: "100%", marginBottom: 10 }}
      />
      <div className="row">
        <button disabled={approving} onClick={() => onApprove(true, note)}>
          {approving ? <span className="spinner on-accent" /> : <IconCheck size={15} />}
          Approve and continue
        </button>
        <button className="danger" disabled={approving} onClick={() => onApprove(false, note)}>
          <IconX size={15} />
          Reject
        </button>
      </div>
    </div>
  );
}
