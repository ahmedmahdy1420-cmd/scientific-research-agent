import type { ReactNode } from "react";

export function Badge({ kind = "info", children }: { kind?: string; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>;
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

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return <div className="error">{message}</div>;
}

export function Spinner({ label = "Working…" }: { label?: string }) {
  return <p className="muted">{label}</p>;
}

export function formatMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function formatCost(usd: number): string {
  return usd === 0 ? "$0.00" : `$${usd.toFixed(4)}`;
}
