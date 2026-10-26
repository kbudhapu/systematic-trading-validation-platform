/**
 * Typed, defensive reader for experiment_artifacts.artifact_json (schema v2).
 *
 * The publisher (scripts/publish_experiment_artifact.py) reads data/research_vault.db and emits the
 * shape below. Every number rendered at /experiments is EXTRACTED IN PYTHON and stored here — this
 * file only picks keys and coerces types. NOTHING here computes a statistic (B4 rule). The old v1
 * honesty/psd-plateau/wf shape does not exist in the vault and is gone.
 *
 * Shape (docs/experiment_artifact_schema.md v2):
 *   experiment_id, title, kind, status, verdict (enum|null), verdict_detail, provenance, as_of,
 *   criteria_md, trials[]{ trial_key, family, hypothesis, instrument, n_events, triage_verdict,
 *   p_value, stats{ sharpe_1x, sharpe_2x, sharpe_4x, stress_2x, stress_4x, ci[lo,hi], ci_kind,
 *   mcpt_p, dsr, *_key } }, psd{ grid_points, timeframes, objective_variants, n_trials, entries }
 */

export type Num = number | null;

export function num(v: unknown): Num {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

function str(v: unknown): string | undefined {
  return v === null || v === undefined ? undefined : String(v);
}

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

/** Family-normalized cost-stress statistics for one trial (all pre-computed in Python). */
export interface TrialStats {
  sharpe1x: Num;
  sharpe1xKey?: string;
  sharpe2x: Num;
  sharpe4x: Num;
  stress2x: Num; // MEAN cost-stress (XSEC/EVENT families) — distinct units from the Sharpe ladder
  stress4x: Num;
  ci: [Num, Num] | null;
  ciKind?: string; // "sharpe" | "mean"
  mcptP: Num;
  mcptPKey?: string;
  dsr: Num;
  nSessions: Num; // present for only ~2/29 trials — renders as an honest gap elsewhere
}

export interface Trial {
  trialKey?: string;
  family?: string;
  hypothesis?: string;
  instrument?: string;
  nEvents: Num;
  triageVerdict?: string;
  pValue: Num;
  stats: TrialStats;
}

export interface PsdBudget {
  gridPoints: Num;
  timeframes: Num;
  objectiveVariants: Num;
  nTrials: Num;
  entries: Num;
}

export interface Artifact {
  experimentId?: string;
  title?: string;
  kind?: string;
  status?: string;
  verdict?: string; // normalized enum, or undefined
  verdictDetail?: string; // raw registry prose
  provenance?: string;
  asOf?: string;
  criteriaMd: string;
  trials: Trial[];
  psd: PsdBudget | null;
}

function parseCi(v: unknown): [Num, Num] | null {
  if (Array.isArray(v) && v.length === 2) return [num(v[0]), num(v[1])];
  return null;
}

function parseStats(raw: unknown): TrialStats {
  const s = obj(raw);
  return {
    sharpe1x: num(s.sharpe_1x),
    sharpe1xKey: str(s.sharpe_1x_key),
    sharpe2x: num(s.sharpe_2x),
    sharpe4x: num(s.sharpe_4x),
    stress2x: num(s.stress_2x),
    stress4x: num(s.stress_4x),
    ci: parseCi(s.ci),
    ciKind: str(s.ci_kind),
    mcptP: num(s.mcpt_p),
    mcptPKey: str(s.mcpt_p_key),
    dsr: num(s.dsr),
    nSessions: num(s.n_sessions),
  };
}

/**
 * T2 — the publisher-computed _META-ANALYTICS artifact (analytics key). ALL aggregation
 * (medians, pass rates, histogram bins, ladder slopes) was computed in Python; this parser
 * only picks keys and coerces types.
 */
export interface FamilyScoreRow {
  family: string;
  nTrials: number;
  nPass: number;
  passRate: Num;
  medianSharpe1x: Num;
  sharpeN: number;
  medianMcptP: Num;
  mcptN: number;
}

export interface MetaAnalytics {
  nTrials: number;
  scorecard: FamilyScoreRow[];
  pHistogram: { binWidth: number; bins: number[]; n: number; uniformRef: Num; killGate: Num };
  scatter: {
    withLadder: { experimentId?: string; trialKey?: string; family?: string; triageVerdict?: string; sharpe1x: Num; ladderSlope: Num }[];
    oneXOnly: { experimentId?: string; trialKey?: string; family?: string; triageVerdict?: string; sharpe1x: Num }[];
  };
  funnel: {
    points: { experimentId?: string; trialKey?: string; family?: string; triageVerdict?: string; sharpe1x: Num; nEvents: Num }[];
    nSessionsCoverage: number;
  };
}

export function parseMetaAnalytics(raw: unknown): MetaAnalytics | null {
  const j = obj(raw);
  const an = obj(j.analytics);
  if (!an || Object.keys(an).length === 0) return null;
  const sc = Array.isArray(an.family_scorecard) ? (an.family_scorecard as unknown[]) : [];
  const ph = obj(an.p_histogram);
  const scat = obj(an.cost_decay_scatter);
  const fun = obj(an.effect_size_funnel);
  const pt = (p: unknown) => {
    const o = obj(p);
    return {
      experimentId: str(o.experiment_id),
      trialKey: str(o.trial_key),
      family: str(o.family),
      triageVerdict: str(o.triage_verdict),
      sharpe1x: num(o.sharpe_1x),
      ladderSlope: num(o.ladder_slope),
      nEvents: num(o.n_events),
    };
  };
  return {
    nTrials: Number(an.n_trials ?? 0),
    scorecard: sc.map((r) => {
      const o = obj(r);
      return {
        family: String(o.family ?? "?"),
        nTrials: Number(o.n_trials ?? 0),
        nPass: Number(o.n_pass ?? 0),
        passRate: num(o.pass_rate),
        medianSharpe1x: num(o.median_sharpe_1x),
        sharpeN: Number(o.sharpe_n ?? 0),
        medianMcptP: num(o.median_mcpt_p),
        mcptN: Number(o.mcpt_n ?? 0),
      };
    }),
    pHistogram: {
      binWidth: Number(ph.bin_width ?? 0.05),
      bins: Array.isArray(ph.bins) ? (ph.bins as unknown[]).map((b) => Number(b) || 0) : [],
      n: Number(ph.n ?? 0),
      uniformRef: num(ph.uniform_ref_per_bin),
      killGate: num(ph.kill_gate),
    },
    scatter: {
      withLadder: (Array.isArray(scat.with_ladder) ? (scat.with_ladder as unknown[]) : []).map(pt),
      oneXOnly: (Array.isArray(scat.one_x_only) ? (scat.one_x_only as unknown[]) : []).map(pt),
    },
    funnel: {
      points: (Array.isArray(fun.points) ? (fun.points as unknown[]) : []).map(pt),
      nSessionsCoverage: Number(fun.n_sessions_coverage ?? 0),
    },
  };
}

export function parseArtifact(raw: unknown): Artifact {
  const j = obj(raw);

  const trialsRaw = Array.isArray(j.trials) ? (j.trials as unknown[]) : [];
  const trials: Trial[] = trialsRaw.map((t) => {
    const to = obj(t);
    return {
      trialKey: str(to.trial_key),
      family: str(to.family),
      hypothesis: str(to.hypothesis),
      instrument: str(to.instrument),
      nEvents: num(to.n_events),
      triageVerdict: str(to.triage_verdict),
      pValue: num(to.p_value),
      stats: parseStats(to.stats),
    };
  });

  let psd: PsdBudget | null = null;
  if (j.psd && typeof j.psd === "object") {
    const p = obj(j.psd);
    psd = {
      gridPoints: num(p.grid_points),
      timeframes: num(p.timeframes),
      objectiveVariants: num(p.objective_variants),
      nTrials: num(p.n_trials),
      entries: num(p.entries),
    };
  }

  return {
    experimentId: str(j.experiment_id),
    title: str(j.title),
    kind: str(j.kind),
    status: str(j.status),
    verdict: str(j.verdict),
    verdictDetail: str(j.verdict_detail),
    provenance: str(j.provenance),
    asOf: str(j.as_of),
    criteriaMd: str(j.criteria_md) ?? "",
    trials,
    psd,
  };
}
