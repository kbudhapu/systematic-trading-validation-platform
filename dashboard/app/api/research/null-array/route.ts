import { noCacheJson } from "@/lib/no-cache";
import { createClient } from "@/lib/supabase/server";
import { parseNumArray } from "@/lib/evidence";

export const dynamic = "force-dynamic";

/**
 * Lazy-load one MCPT null array by source_row_id. The listing/detail pages SELECT metadata only
 * (EVIDENCE_META_COLS excludes null_array); a present-histogram panel fetches its ~1000-element
 * array from here when it opens, so the initial page payload stays light.
 *
 * Render-only: returns the stored array verbatim (parsed to numbers), plus the row's own
 * checksum (artifact_hash) so the panel can caption the verdict's OWN null, never a recomputation.
 */
export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const id = Number.parseInt(searchParams.get("id") ?? "", 10);
  if (!Number.isFinite(id)) {
    return noCacheJson({ error: "id (source_row_id) required" }, { status: 400 });
  }

  const supabase = await createClient();
  const { data, error } = await supabase
    .from("research_evidence")
    .select("source_row_id, array_state, null_array, artifact_hash, n_perm")
    .eq("source_row_id", id)
    .limit(1)
    .maybeSingle();

  if (error) {
    return noCacheJson({ error: error.message }, { status: 500 });
  }
  if (!data) {
    return noCacheJson({ error: "not found" }, { status: 404 });
  }
  if (data.array_state !== "present" || data.null_array == null) {
    // deferred / absent rows have no array to show — say so, never fabricate one.
    return noCacheJson({
      sourceRowId: id,
      arrayState: data.array_state,
      nullArray: null,
      artifactHash: data.artifact_hash ?? null,
      nPerm: data.n_perm ?? null,
    });
  }

  return noCacheJson({
    sourceRowId: id,
    arrayState: "present",
    nullArray: parseNumArray(data.null_array),
    artifactHash: data.artifact_hash ?? null,
    nPerm: data.n_perm ?? null,
  });
}
