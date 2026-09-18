import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, RequestError } from "../lib/api";
import { ErrorBanner, Spinner } from "../components/Common";
import { RunDetail } from "../components/RunDetail";
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
      <h2>Agent Run</h2>
      <p className="subtitle">
        <Link to="/runs">&larr; All runs</Link>
      </p>
      <ErrorBanner message={error} />
      {!run && !error && <Spinner label="Loading run…" />}
      {run && <RunDetail run={run} onApprove={decide} approving={approving} />}
    </>
  );
}
