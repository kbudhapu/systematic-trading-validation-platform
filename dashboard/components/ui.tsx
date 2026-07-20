"use client";

import Link from "next/link";

export function EnvBanner({ environment }: { environment: string }) {
  const isLive = environment === "live";
  return (
    <div
      className={`w-full py-2 text-center text-sm font-bold tracking-widest ${
        isLive ? "bg-live text-red-100" : "bg-paper text-blue-100"
      }`}
    >
      {isLive ? "LIVE TRADING — REAL MONEY" : "PAPER TRADING — SIMULATED"}
    </div>
  );
}

export function Card({
  title,
  children,
  className = "",
}: {
  title?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-lg border border-[var(--border)] bg-[var(--card)] p-4 ${className}`}
    >
      {title && (
        <h2 className="mb-3 text-sm font-medium text-[var(--muted)] uppercase tracking-wide">
          {title}
        </h2>
      )}
      {children}
    </div>
  );
}

export function Stat({
  label,
  value,
  positive,
}: {
  label: string;
  value: string;
  positive?: boolean;
}) {
  const color =
    positive === undefined
      ? "text-[var(--text)]"
      : positive
        ? "text-[var(--green)]"
        : "text-[var(--red)]";
  return (
    <div>
      <div className="text-xs text-[var(--muted)]">{label}</div>
      <div className={`text-xl font-semibold ${color}`}>{value}</div>
    </div>
  );
}

export function NavLink({
  href,
  children,
  active,
  className = "",
}: {
  href: string;
  children: React.ReactNode;
  active?: boolean;
  className?: string;
}) {
  return (
    <Link
      href={href}
      className={`block rounded px-3 py-2 text-sm ${
        active
          ? "bg-[var(--border)] text-white"
          : "text-[var(--muted)] hover:text-white"
      } ${className}`}
    >
      {children}
    </Link>
  );
}
