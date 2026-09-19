/**
 * Shared UI primitives.
 *
 * Everything here is presentational and dependency-free: the components map
 * backend vocabulary (a status string, a latency, a dollar figure) onto the
 * design tokens in styles.css.
 */

import { useEffect, useState, type ReactNode } from "react";
import { IconAlert, IconCheck, IconInfo, IconMoon, IconSearch, IconSun } from "./Icons";

export function Badge({
  kind = "info",
  plain = false,
  children,
}: {
  kind?: string;
  plain?: boolean;
  children: ReactNode;
}) {
  return <span className={`badge ${kind}${plain ? " plain" : ""}`}>{children}</span>;
}

/** Maps a backend status string onto a colour. */
export function statusKind(status: string): string {
  if (["completed", "executed", "ok", "accept"].includes(status)) return "ok";
  if (["awaiting_approval", "answer_with_caveats", "degraded", "processing"].includes(status))
    return "warn";
  if (["failed", "rejected", "denied", "refuse", "timeout", "budget_exceeded"].includes(status))
    return "bad";
  return "info";
}

export function Stat({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: ReactNode;
  accent?: boolean;
}) {
  return (
    <div className={`stat${accent ? " accent" : ""}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

/** A panel with an uppercase, icon-led header. */
export function Card({
  title,
  icon,
  actions,
  flush = false,
  className = "",
  children,
}: {
  title?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  flush?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`card${flush ? " flush" : ""} ${className}`.trim()}>
      {(title || actions) && (
        <header className="card-head">
          <div className="title">
            {icon}
            {title}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function PageHead({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-head fade-up">
      <div className="row between">
        <h2>{title}</h2>
        {actions}
      </div>
      <p className="subtitle">{subtitle}</p>
    </header>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="banner error" role="alert">
      <IconAlert size={16} />
      <span>{message}</span>
    </div>
  );
}

export function NoticeBanner({ message, kind = "notice" }: { message: string; kind?: string }) {
  return (
    <div className={`banner ${kind}`}>
      {kind === "notice" ? <IconInfo size={16} /> : <IconCheck size={16} />}
      <span>{message}</span>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="row muted" style={{ padding: "6px 0" }}>
      <span className="spinner" />
      {label && <span>{label}</span>}
    </div>
  );
}

/** Shimmering placeholder rows, so a slow list does not collapse the layout. */
export function SkeletonRows({ rows = 4, height = 34 }: { rows?: number; height?: number }) {
  return (
    <div className="stack" style={{ gap: 8 }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height, opacity: 1 - i * 0.14 }} />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  icon,
}: {
  title: string;
  hint?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="glyph">{icon ?? <IconSearch size={20} />}</div>
      <strong>{title}</strong>
      {hint && <span>{hint}</span>}
    </div>
  );
}

/** Dark/light switch. The initial value is set in index.html before paint. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<string>(
    () => document.documentElement.dataset.theme ?? "dark",
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("sra-theme", theme);
    } catch {
      /* private mode: the choice just does not persist */
    }
  }, [theme]);

  return (
    <button
      type="button"
      className="ghost icon"
      title={theme === "dark" ? "Switch to light" : "Switch to dark"}
      aria-label="Toggle colour theme"
      onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
    >
      {theme === "dark" ? <IconSun size={16} /> : <IconMoon size={16} />}
    </button>
  );
}

export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function formatCost(usd: number): string {
  return usd === 0 ? "$0.00" : `$${usd.toFixed(4)}`;
}

/** "3 minutes ago" for recent rows, an absolute date once that stops helping. */
export function formatWhen(iso: string): string {
  const then = new Date(iso);
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join("");
}
