import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, RequestError } from "../lib/api";
import { Card, ErrorBanner, PageHead, SkeletonRows } from "../components/Common";
import { RunDetail } from "../components/RunDetail";
import { IconArrowLeft } from "../components/Icons";
import type { AgentRun } from "../lib/types";

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);

  const load = useCallback(async () => {
    if (!runId) return;
    try {
      setRun(await api.getRun(runId));
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "Could not load the run");
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(approved: boolean, note: string) {
    if (!runId) return;
    setApproving(true);
    try {
      setRun(await api.decideApproval(runId, approved, note));
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "Approval failed");
    } finally {
      setApproving(false);
    }
  }

  return (
    <>
      <PageHead
        title="Agent Run"
        subtitle={run?.question ?? "Loading the recorded trace for this run."}
        actions={
          <Link to="/runs" className="btn secondary">
            <IconArrowLeft size={14} />
            All runs
          </Link>
        }
      />

      <ErrorBanner message={error} />

      {!run && !error && (
        <Card>
          <SkeletonRows rows={6} height={40} />
        </Card>
      )}

      {run && <RunDetail run={run} onApprove={decide} approving={approving} />}
    </>
  );
}
