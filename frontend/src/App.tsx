import { useCallback, useEffect, useState } from "react";
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Route,
  Routes,
} from "react-router-dom";
import { api, getToken } from "./lib/api";
import type { CurrentUser } from "./lib/types";
import Assistant from "./pages/Assistant";
import Documents from "./pages/Documents";
import Evaluation from "./pages/Evaluation";
import Login from "./pages/Login";
import RunDetailPage from "./pages/RunDetailPage";
import Runs from "./pages/Runs";
import { Badge } from "./components/Common";

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);

  const loadUser = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setChecked(true);
      return;
    }
    try {
      setUser(await api.me());
    } catch {
      setUser(null);
    } finally {
      setChecked(true);
    }
  }, []);

  useEffect(() => {
    void loadUser();
  }, [loadUser]);

  if (!checked) return <div className="login-wrap"><p className="muted">Loading…</p></div>;
  if (!user) return <Login onLoggedIn={loadUser} />;

  return (
    <BrowserRouter>
      <div className="layout">
        <aside className="sidebar">
          <h1>Research Agent</h1>
          <div className="tagline">FastAPI · LangGraph · pgvector · MCP</div>
          <nav>
            <NavLink to="/" end>Assistant</NavLink>
            <NavLink to="/runs">Agent runs</NavLink>
            <NavLink to="/documents">Documents</NavLink>
            <NavLink to="/evaluation">Evaluation</NavLink>
          </nav>
          <div style={{ marginTop: 28 }}>
            <div className="muted" style={{ fontSize: 11 }}>SIGNED IN AS</div>
            <div>{user.full_name}</div>
            <div className="muted mono" style={{ fontSize: 11 }}>{user.email}</div>
            <div style={{ marginTop: 6 }}>
              {user.roles.map((role) => (
                <Badge key={role.id}>{role.name}</Badge>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
              clearance: {user.max_access_level}
            </div>
            <button
              className="secondary"
              style={{ marginTop: 12, width: "100%" }}
              onClick={() => {
                api.logout();
                setUser(null);
              }}
            >
              Sign out
            </button>
          </div>
        </aside>

        <main className="main">
          <Routes>
            <Route path="/" element={<Assistant />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:runId" element={<RunDetailPage />} />
            <Route path="/documents" element={<Documents />} />
            <Route path="/evaluation" element={<Evaluation />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
