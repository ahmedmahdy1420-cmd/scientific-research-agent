import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, RequestError } from "../lib/api";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHead,
  SkeletonRows,
  Stat,
  formatCost,
  formatMs,
  formatWhen,
  statusKind,
} from "../components/Common";
import { IconArrowRight, IconRuns, IconSearch } from "../components/Icons";
import type { AgentRunSummary } from "../lib/types";

export default function Runs() {
  const [runs, setRuns] = useState<AgentRunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    api
      .listRuns()
      .then((page) => setRuns(page.items))
      .catch((err) =>
        setError(err instanceof RequestError ? err.message : "Could not load runs"),
      );
  }, []);

  const visible = useMemo(() => {
    if (!runs) return [];
    const needle = filter.trim().toLowerCase();
    if (!needle) return runs;
    return runs.filter(
      (run) =>
        run.question.toLowerCase().includes(needle) ||
        run.status.includes(needle) ||
        (run.category ?? "").includes(needle),
    );
  }, [runs, filter]);

  const totals = useMemo(() => {
    if (!runs || runs.length === 0) return null;
    return {
      count: runs.length,
      cost: runs.reduce((sum, run) => sum + run.estimated_cost_usd, 0),
      latency: Math.round(
        runs.reduce((sum, run) => sum + run.total_latency_ms, 0) / runs.length,
      ),
      tools: runs.reduce((sum, run) => sum + run.tool_calls_used, 0),
    };
  }, [runs]);

  return (
    <>
      <PageHead
        title="Agent Runs"
        subtitle="Every run is persisted with its plan, its tool calls, the evidence it retrieved and what it cost — so any answer can be reconstructed long after it was given."
      />

      <ErrorBanner message={error} />

      {totals && (
        <div className="stats summary fade-up" style={{ marginBottom: 16 }}>
          <Stat accent label="Runs" value={totals.count} />
          <Stat label="Mean latency" value={formatMs(totals.latency)} />
          <Stat label="Tool calls" value={totals.tools} />
          <Stat accent label="Total cost" value={formatCost(totals.cost)} />
        </div>
      )}

      {!runs && !error && (
        <Card title="Loading" icon={<IconRuns size={14} />}>
          <SkeletonRows rows={5} />
        </Card>
      )}

      {runs && runs.length === 0 && (
        <Card>
          <EmptyState
            icon={<IconRuns size={20} />}
            title="No runs yet"
            hint={
              <>
                Ask something on the <Link to="/">Assistant</Link> page and it will show up here.
              </>
            }
          />
        </Card>
      )}

      {runs && runs.length > 0 && (
        <Card
          flush
          title={`History (${visible.length})`}
          icon={<IconRuns size={14} />}
          actions={
            <div className="row" style={{ gap: 6, position: "relative" }}>
              <IconSearch
                size={14}
                className="faint"
                style={{ position: "absolute", left: 10, pointerEvents: "none" }}
              />
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter runs…"
                style={{ paddingLeft: 30, width: 200 }}
              />
            </div>
          }
        >
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Question</th>
                  <th>Status</th>
                  <th>Category</th>
                  <th>Tools</th>
                  <th>Latency</th>
                  <th>Cost</th>
                  <th>When</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {visible.map((run) => (
                  <tr key={run.id}>
                    <td style={{ maxWidth: 360 }}>
                      <Link to={`/runs/${run.id}`}>
                        {run.question.length > 76
                          ? `${run.question.slice(0, 76)}…`
                          : run.question}
                      </Link>
                    </td>
                    <td>
                      <Badge kind={statusKind(run.status)}>{run.status.replace(/_/g, " ")}</Badge>
                    </td>
                    <td className="muted">{run.category ?? "—"}</td>
                    <td className="num">{run.tool_calls_used}</td>
                    <td className="num">{formatMs(run.total_latency_ms)}</td>
                    <td className="num">{formatCost(run.estimated_cost_usd)}</td>
                    <td className="muted tiny" title={new Date(run.created_at).toLocaleString()}>
                      {formatWhen(run.created_at)}
                    </td>
                    <td>
                      <Link to={`/runs/${run.id}`} aria-label="Open run">
                        <IconArrowRight size={14} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {visible.length === 0 && (
            <EmptyState title="Nothing matches that filter" hint="Try a shorter term." />
          )}
        </Card>
      )}
    </>
  );
}
