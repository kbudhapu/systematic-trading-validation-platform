export type TimeRange = "1D" | "1M" | "1Y" | "YTD";

export function rangeToDate(range: TimeRange): string {
  const now = new Date();
  switch (range) {
    case "1D":
      return new Date(now.getTime() - 86400000).toISOString();
    case "1M":
      return new Date(now.getTime() - 30 * 86400000).toISOString();
    case "1Y":
      return new Date(now.getTime() - 365 * 86400000).toISOString();
    case "YTD":
      return new Date(now.getFullYear(), 0, 1).toISOString();
  }
}

export function formatPct(n: number): string {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

export function formatUsd(n: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

export interface EquitySnapshot {
  recorded_at: string;
  equity: number;
  pct_return: number;
}

export interface Strategy {
  id: string;
  name: string;
  module: string;
  symbol: string;
  enabled: boolean;
  environment: string;
  params: Record<string, number>;
  version_id: number;
}

export interface BotRun {
  status: string;
  message: string;
  equity: number;
  drawdown_pct: number;
  cycle_ms: number;
  created_at: string;
  halted: boolean;
}

export interface BacktestRun {
  id: string;
  sharpe: number;
  max_drawdown: number;
  total_return: number;
  total_trades: number;
  passed: boolean;
  flags: string[];
  started_at: string;
  equity_curve: number[];
  strategy_id?: string;
}

export interface LegPerformance {
  id: string;
  name: string;
  symbol: string;
  enabled: boolean;
  environment: string;
  latestPct: number;
  latestEquity: number;
  sessionId?: string;
  snapshots: EquitySnapshot[];
}

export interface StrategyLegData {
  strategy: Strategy;
  backtest: BacktestRun | null;
  paperDays: number;
  paperReturn: number;
}
