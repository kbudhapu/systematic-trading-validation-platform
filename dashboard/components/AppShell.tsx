"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { EnvBanner } from "@/components/ui";

const NAV = [
  { href: "/portfolio", label: "Portfolio" },
  { href: "/legs", label: "Legs" },
  { href: "/experiments", label: "Experiments" },
  { href: "/alerts", label: "Alerts" },
  { href: "/pipeline", label: "Pipeline" },
  { href: "/ops", label: "Ops" },
  { href: "/controls", label: "Controls" },
];

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(href + "/");
}

function NavItem({
  href,
  label,
  active,
  className = "",
}: {
  href: string;
  label: string;
  active: boolean;
  className?: string;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`block rounded px-3 py-2 text-sm transition-colors ${
        active
          ? "bg-[var(--accent-soft)] text-[var(--text)]"
          : "text-[var(--muted)] hover:text-[var(--text)]"
      } ${className}`}
    >
      {label}
    </Link>
  );
}

export function AppShell({
  children,
  environment,
  userEmail,
}: {
  children: React.ReactNode;
  environment: string;
  userEmail: string;
}) {
  const pathname = usePathname() ?? "";

  return (
    <div className="min-h-screen">
      <EnvBanner environment={environment} />

      {/* Mobile top nav */}
      <nav className="flex items-center gap-1 overflow-x-auto border-b border-[var(--border)] px-3 py-2 md:hidden">
        {NAV.map((item) => (
          <NavItem
            key={item.href}
            href={item.href}
            label={item.label}
            active={isActive(pathname, item.href)}
            className="shrink-0 whitespace-nowrap"
          />
        ))}
        <form action="/auth/signout" method="post" className="ml-auto shrink-0">
          <button
            type="submit"
            className="rounded px-3 py-2 text-sm text-[var(--muted)] hover:text-[var(--text)]"
          >
            Sign out
          </button>
        </form>
      </nav>

      <div className="flex">
        {/* Desktop side nav */}
        <aside className="hidden w-52 shrink-0 border-r border-[var(--border)] p-4 md:block">
          <div className="mb-1 text-sm font-semibold tracking-tight text-[var(--text)]">
            mbappe
          </div>
          <div className="mb-6 truncate text-xs text-[var(--muted)]" title={userEmail}>
            {userEmail || "—"}
          </div>
          <nav className="space-y-1">
            {NAV.map((item) => (
              <NavItem
                key={item.href}
                href={item.href}
                label={item.label}
                active={isActive(pathname, item.href)}
              />
            ))}
          </nav>
          <form action="/auth/signout" method="post" className="mt-6">
            <button
              type="submit"
              className="text-sm text-[var(--muted)] hover:text-[var(--text)]"
            >
              Sign out
            </button>
          </form>
        </aside>

        <main className="mx-auto w-full max-w-5xl flex-1 p-4 md:p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
