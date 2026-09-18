import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, RequestError } from "../lib/api";
import { ErrorBanner, Spinner } from "../components/Common";
import { RunDetail } from "../components/RunDetail";
import type { AgentRun } from "../lib/types";

const EXAMPLES = [
  "Find recent research about breast cancer biomarkers and summarise the strongest evidence.",
  "Find clinical trials related to type 2 diabetes and compare their phases.",
  "Which experiments are associated with compound CMP-0001?",
  "Search scientific documents and internal research data about PARP inhibition, then compare their conclusions.",
  "Delete the document about breast cancer biomarkers from the corpus.",
];

export default function Assistant() {
  const [question, setQuestion] = useState(EXAMPLES[0]);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [approving, setApproving] = useState(false);
  const navigate = useNavigate();

  async function ask(event?: React.FormEvent) {
    event?.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    setRun(null);
    try {
      setRun(await api.chat(question));
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "The request failed");
    } finally {
      setBusy(false);
    }
  }

  async function decide(approved: boolean, note: string) {
    if (!run) return;
    setApproving(true);
    setError(null);
    try {
      setRun(await api.decideApproval(run.run_id, approved, note));
    } catch (err) {
      setError(
        err instanceof RequestError
          ? `${err.message}${err.code === "authorization_failed" ? " (sign in as another reviewer)" : ""}`
          : "Approval failed",
      );
    } finally {
      setApproving(false);
    }
  }

  return (
    <>
      <h2>Research Assistant</h2>
      <p className="subtitle">
        Ask a question. The agent classifies it, plans typed tool calls, runs the authorised
        ones, then verifies its own answer against the evidence before returning it.
      </p>

      <ErrorBanner message={error} />

      <form className="card" onSubmit={ask}>
        <textarea
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. What does the literature say about HER2 in breast cancer?"
        />
        <div className="row" style={{ marginTop: 10 }}>
          <button type="submit" disabled={busy}>
            {busy ? "Running…" : "Ask"}
          </button>
          {run && (
            <button
              type="button"
              className="secondary"
              onClick={() => navigate(`/runs/${run.run_id}`)}
            >
              Open run details
            </button>
          )}
        </div>

        <h3>Demo scenarios</h3>
        {EXAMPLES.map((example) => (
          <div key={example} style={{ marginBottom: 6 }}>
            <button
              type="button"
              className="secondary"
              style={{ fontSize: 12, padding: "5px 9px", textAlign: "left" }}
              onClick={() => setQuestion(example)}
            >
              {example}
            </button>
          </div>
        ))}
      </form>

      {busy && <Spinner label="Classifying, planning, retrieving, verifying…" />}
      {run && <RunDetail run={run} onApprove={decide} approving={approving} />}
    </>
  );
}
