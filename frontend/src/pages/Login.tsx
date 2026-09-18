import { useState } from "react";
import { api, RequestError } from "../lib/api";
import { ErrorBanner } from "../components/Common";

const DEMO_ACCOUNTS = [
  ["researcher@example.com", "researcher", "public + internal material"],
  ["senior@example.com", "senior_researcher", "also restricted; can approve"],
  ["admin@example.com", "admin", "everything, including sensitive tools"],
];

export default function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState("researcher@example.com");
  const [password, setPassword] = useState("Research123!");
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
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <h2>Scientific Research Agent</h2>
        <p className="subtitle">Sign in to run the research assistant.</p>
        <ErrorBanner message={error} />
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="Email"
          autoComplete="username"
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoComplete="current-password"
        />
        <button type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <h3>Demo accounts</h3>
        <table>
          <tbody>
            {DEMO_ACCOUNTS.map(([addr, role, note]) => (
              <tr key={addr}>
                <td>
                  <button
                    type="button"
                    className="secondary"
                    style={{ padding: "3px 8px", fontSize: 12 }}
                    onClick={() => setEmail(addr)}
                  >
                    use
                  </button>
                </td>
                <td className="mono">{role}</td>
                <td className="muted">{note}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted" style={{ marginBottom: 0 }}>
          Password for all three: <span className="mono">Research123!</span>
        </p>
      </form>
    </div>
  );
}
