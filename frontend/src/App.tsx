import { useCallback, useEffect, useState } from "react";
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { api, getToken } from "./lib/api";
import type { CurrentUser } from "./lib/types";
import Assistant from "./pages/Assistant";
import Documents from "./pages/Documents";
import Evaluation from "./pages/Evaluation";
import Login from "./pages/Login";
import RunDetailPage from "./pages/RunDetailPage";
import Runs from "./pages/Runs";
import { Badge, ThemeToggle, initials } from "./components/Common";
import {
  IconDocument,
  IconFlask,
  IconGauge,
  IconLogout,
  IconRuns,
  IconSpark,
} from "./components/Icons";

const NAV = [
  { to: "/", label: "Assistant", icon: IconSpark, end: true },
  { to: "/runs", label: "Agent runs", icon: IconRuns, end: false },
  { to: "/documents", label: "Documents", icon: IconDocument, end: false },
  { to: "/evaluation", label: "Evaluation", icon: IconGauge, end: false },
];

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

  if (!checked) return <BootScreen />;
  if (!user) return <Login onLoggedIn={loadUser} />;

  return (
    <BrowserRouter>
      <div className="layout">
        <Sidebar user={user} onSignOut={() => { api.logout(); setUser(null); }} />
        <div className="main">
          <Topbar />
          <div className="page">
            <Routes>
              <Route path="/" element={<Assistant />} />
              <Route path="/runs" element={<Runs />} />
              <Route path="/runs/:runId" element={<RunDetailPage />} />
              <Route path="/documents" element={<Documents />} />
              <Route path="/evaluation" element={<Evaluation />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </div>
      </div>
    </BrowserRouter>
  );
}

/** Shown for the one round trip it takes to resolve the stored token. */
function BootScreen() {
  return (
    <div style={{ display: "grid", placeItems: "center", minHeight: "100vh" }}>
      <div className="stack" style={{ alignItems: "center", gap: 14 }}>
        <div className="brand">
          <div className="mark">
            <IconFlask size={19} />
          </div>
        </div>
        <span className="muted">Restoring your session…</span>
      </div>
    </div>
  );
}

function Sidebar({ user, onSignOut }: { user: CurrentUser; onSignOut: () => void }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="mark">
          <IconFlask size={19} />
        </div>
        <div>
          <h1>Research Agent</h1>
          <div className="tagline">LangGraph · pgvector · MCP</div>
        </div>
      </div>

      <nav>
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end}>
            <Icon size={16} />
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="spacer" />

      <div className="user-card">
        <div className="row" style={{ gap: 10, flexWrap: "nowrap" }}>
          <div className="avatar">{initials(user.full_name)}</div>
          <div className="stack" style={{ minWidth: 0, gap: 0 }}>
            <strong style={{ fontSize: 13 }}>{user.full_name}</strong>
            <span
              className="muted mono tiny"
              style={{ overflow: "hidden", textOverflow: "ellipsis" }}
            >
              {user.email}
            </span>
          </div>
        </div>

        <div className="row" style={{ gap: 6, marginTop: 10 }}>
          {user.roles.map((role) => (
            <Badge key={role.id} kind="info">
              {role.name}
            </Badge>
          ))}
        </div>

        <div className="row between tiny muted" style={{ marginTop: 8 }}>
          <span>clearance</span>
          <span className="mono">{user.max_access_level}</span>
        </div>

        <button className="secondary block small" style={{ marginTop: 11 }} onClick={onSignOut}>
          <IconLogout size={14} />
          Sign out
        </button>
      </div>
    </aside>
  );
}

function Topbar() {
  const { pathname } = useLocation();
  const current =
    NAV.find((item) => (item.end ? pathname === item.to : pathname.startsWith(item.to)))?.label ??
    "Agent run";

  return (
    <header className="topbar">
      <span className="crumb">
        Research Agent <span style={{ opacity: 0.4 }}>/</span>{" "}
        <span style={{ color: "var(--text)" }}>{current}</span>
      </span>
      <div className="grow" />
      <span className="badge ok" title="The API answered /me, so the session is live">
        connected
      </span>
      <ThemeToggle />
    </header>
  );
}
