import { useState } from "react";
import { api, RequestError } from "../lib/api";
import { ErrorBanner } from "../components/Common";
import {
  IconArrowRight,
  IconFlask,
  IconGraph,
  IconQuote,
  IconShield,
} from "../components/Icons";

const PASSWORD = "Research123!";

const DEMO_ACCOUNTS = [
  {
    email: "researcher@example.com",
    role: "researcher",
    note: "public + internal material",
  },
  {
    email: "senior@example.com",
    role: "senior_researcher",
    note: "also restricted; can approve",
  },
  {
    email: "admin@example.com",
    role: "admin",
    note: "everything, including sensitive tools",
  },
];

const FEATURES = [
  {
    icon: IconGraph,
    title: "A bounded agent, not a chat box",
    body: "LangGraph plans typed tool calls under hard iteration, tool and wall-clock budgets.",
  },
  {
    icon: IconQuote,
    title: "Every claim traced to evidence",
    body: "Answers are verified against the retrieved passages before they are returned to you.",
  },
  {
    icon: IconShield,
    title: "The model is not the security boundary",
    body: "Permissions, clearance filters and human approval are enforced in application code.",
  },
];

export default function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState(DEMO_ACCOUNTS[0]!.email);
  const [password, setPassword] = useState(PASSWORD);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      onLoggedIn();
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <section className="hero">
        <div className="brand" style={{ padding: 0 }}>
          <div className="mark">
            <IconFlask size={19} />
          </div>
          <div>
            <h1 style={{ fontSize: 14.5 }}>Research Agent</h1>
            <div className="tagline">FastAPI · LangGraph · pgvector · MCP</div>
          </div>
        </div>

        <div>
          <h1>
            Ask a research question.
            <br />
            <span className="gradient-text">Watch the agent show its work.</span>
          </h1>
          <p>
            A scientific assistant over a private corpus, an experiment database and an external
            trials API — where the plan, every tool call, the retrieved evidence and the cost are
            all on the record.
          </p>
        </div>

        <div>
          {FEATURES.map(({ icon: Icon, title, body }) => (
            <div className="feature" key={title}>
              <div className="dot">
                <Icon size={14} />
              </div>
              <div>
                <strong>{title}</strong>
                <span>{body}</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="form-side">
        <form className="fade-up" onSubmit={submit}>
          <div className="eyebrow">Sign in</div>
          <h2 style={{ fontSize: 24, margin: "4px 0 6px" }}>Welcome back</h2>
          <p className="muted" style={{ marginBottom: 20 }}>
            Pick one of the seeded accounts — each one sees a different slice of the corpus.
          </p>

          <ErrorBanner message={error} />

          <div className="stack" style={{ gap: 12, marginBottom: 14 }}>
            <label className="field">
              Email
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="username"
              />
            </label>
            <label className="field">
              Password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                autoComplete="current-password"
              />
            </label>
          </div>

          <button type="submit" className="block" disabled={busy}>
            {busy ? <span className="spinner on-accent" /> : <IconArrowRight size={15} />}
            {busy ? "Signing in…" : "Sign in"}
          </button>

          <div className="eyebrow" style={{ margin: "24px 0 10px" }}>
            Demo accounts
          </div>
          {DEMO_ACCOUNTS.map((account) => (
            <button
              type="button"
              key={account.email}
              className={`account${email === account.email ? " selected" : ""}`}
              onClick={() => {
                setEmail(account.email);
                setPassword(PASSWORD);
              }}
            >
              <span className="avatar" style={{ width: 28, height: 28, flexBasis: 28 }}>
                {account.role[0]!.toUpperCase()}
              </span>
              <span className="stack" style={{ gap: 0 }}>
                <span className="mono" style={{ fontSize: 12.5 }}>
                  {account.role}
                </span>
                <span className="muted tiny">{account.note}</span>
              </span>
            </button>
          ))}

          <p className="muted tiny" style={{ marginTop: 12, marginBottom: 0 }}>
            Password for all three: <span className="mono">{PASSWORD}</span>. These are seeded demo
            users — no real credentials live in this repository.
          </p>
        </form>
      </section>
    </div>
  );
}
