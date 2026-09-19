import { Fragment, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, RequestError } from "../lib/api";
import { Card, ErrorBanner, PageHead } from "../components/Common";
import { RunDetail } from "../components/RunDetail";
import {
  IconArrowRight,
  IconBolt,
  IconGraph,
  IconShield,
  IconSpark,
  IconWand,
} from "../components/Icons";
import type { AgentRun } from "../lib/types";

const EXAMPLES = [
  {
    label: "Summarise the evidence",
    text: "Find recent research about breast cancer biomarkers and summarise the strongest evidence.",
    icon: IconSpark,
  },
  {
    label: "External trials API",
    text: "Find clinical trials related to type 2 diabetes and compare their phases.",
    icon: IconBolt,
  },
  {
    label: "Structured data",
    text: "Which experiments are associated with compound CMP-0001?",
    icon: IconWand,
  },
  {
    label: "Multi-source",
    text: "Search scientific documents and internal research data about PARP inhibition, then compare their conclusions.",
    icon: IconSpark,
  },
  {
    label: "Needs human approval",
    text: "Delete the document about breast cancer biomarkers from the corpus.",
    icon: IconShield,
  },
];

/** The graph's happy path, used only as a progress readout while a run is in
 *  flight. The authoritative node trace comes back on the run itself. */
const STAGES = ["classify", "plan", "select_tools", "execute", "analyze", "verify"];

export default function Assistant() {
  const [question, setQuestion] = useState(EXAMPLES[0]!.text);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [approving, setApproving] = useState(false);
  const [stage, setStage] = useState(0);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const navigate = useNavigate();

  // Advance the progress readout while the request is open. It stalls on the
  // last stage rather than claiming to have finished.
  useEffect(() => {
    if (!busy) return;
    setStage(0);
    const timer = setInterval(
      () => setStage((current) => Math.min(current + 1, STAGES.length - 1)),
      700,
    );
    return () => clearInterval(timer);
  }, [busy]);

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
      <PageHead
        title="Research Assistant"
        subtitle="Ask a question. The agent classifies it, plans typed tool calls, runs the ones you are authorised for, then verifies its own answer against the retrieved evidence before returning it."
      />

      <ErrorBanner message={error} />

      <form className="card accent fade-up" onSubmit={ask}>
        <textarea
          ref={textarea}
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void ask();
          }}
          placeholder="e.g. What does the literature say about HER2 in breast cancer?"
          style={{ background: "transparent", border: "none", padding: 0, fontSize: 15.5 }}
        />

        <div className="row between" style={{ marginTop: 14 }}>
          <span className="faint tiny">
            <kbd className="mono">⌘</kbd> + <kbd className="mono">↵</kbd> to run ·{" "}
            {question.trim().length} characters
          </span>
          <div className="row" style={{ gap: 8 }}>
            {run && (
              <button
                type="button"
                className="secondary"
                onClick={() => navigate(`/runs/${run.run_id}`)}
              >
                Open run details
              </button>
            )}
            <button type="submit" disabled={busy || !question.trim()}>
              {busy ? <span className="spinner on-accent" /> : <IconArrowRight size={15} />}
              {busy ? "Running…" : "Ask the agent"}
            </button>
          </div>
        </div>
      </form>

      <div className="chips" style={{ marginBottom: 20 }}>
        {EXAMPLES.map(({ label, text, icon: Icon }) => (
          <button
            type="button"
            key={label}
            className={`chip${question === text ? " selected" : ""}`}
            title={text}
            onClick={() => {
              setQuestion(text);
              textarea.current?.focus();
            }}
          >
            <Icon size={13} />
            {label}
          </button>
        ))}
      </div>

      {busy && (
        <div className="card fade-up">
          <div className="pipeline">
            {STAGES.map((name, index) => (
              <span
                key={name}
                className={`stage${index < stage ? " done" : index === stage ? " active" : ""}`}
              >
                {index === stage && <span className="spinner" style={{ width: 11, height: 11 }} />}
                {name}
              </span>
            ))}
          </div>
          <p className="faint tiny" style={{ margin: "12px 0 0" }}>
            Indicative progress through the graph — the run returns the node trace it actually
            took, including any retries or budget cut-offs.
          </p>
        </div>
      )}

      {!run && !busy && <IdleHint />}

      {run && <RunDetail run={run} onApprove={decide} approving={approving} />}
    </>
  );
}

/** What the page shows before the first question, instead of dead space. */
function IdleHint() {
  return (
    <Card title="What happens when you ask" icon={<IconGraph size={14} />}>
      <div className="pipeline" style={{ marginBottom: 14 }}>
        {STAGES.map((name, index) => (
          <Fragment key={name}>
            {index > 0 && <span className="arrow">→</span>}
            <span className="stage">{name}</span>
          </Fragment>
        ))}
      </div>
      <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
        <li>
          The plan the model produces is a <strong>proposal</strong>. Tools you are not
          authorised for are dropped before anything executes.
        </li>
        <li>
          Retrieval is filtered by your clearance <strong>in SQL</strong>, so material you may
          not read never reaches the prompt.
        </li>
        <li>
          Every citation is checked against the evidence that was actually returned; a claim that
          cites nothing is not allowed to stand.
        </li>
        <li>
          Anything destructive stops at the approval gate with the graph state checkpointed — and
          the requester cannot approve it.
        </li>
      </ul>
    </Card>
  );
}
