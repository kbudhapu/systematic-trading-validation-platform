import { noCacheJson } from "@/lib/no-cache";
import { requireUser } from "@/lib/supabase/require-user";
import { createDashboardApiClient } from "@/lib/supabase/dashboard-api";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

interface ParamsBody {
  params: Record<string, number>;
  expected_version: number;
}

export async function PATCH(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  if (!user) {
    return noCacheJson({ error: "Unauthorized" }, { status: 401 });
  }

  const { id: strategyId } = await context.params;
  let body: ParamsBody;
  try {
    body = (await request.json()) as ParamsBody;
  } catch {
    return noCacheJson({ error: "invalid_json" }, { status: 400 });
  }

  if (
    body.params === null ||
    typeof body.params !== "object" ||
    typeof body.expected_version !== "number" ||
    !Number.isFinite(body.expected_version)
  ) {
    return noCacheJson({ error: "invalid_payload" }, { status: 400 });
  }

  const supabase = createDashboardApiClient() ?? (await createClient());
  const { data, error } = await supabase.rpc("update_strategy_params_optimistic", {
    p_strategy_id: strategyId,
    p_new_params: body.params,
    p_expected_version: body.expected_version,
  });

  if (error) {
    const isVersionConflict =
      error.code === "40001" ||
      error.message.includes("STRATEGY_VERSION_CONFLICT");
    if (isVersionConflict) {
      return noCacheJson(
        {
          error: "STRATEGY_VERSION_CONFLICT",
          message:
            "Configuration changed elsewhere. Reload the page and retry your edit.",
        },
        { status: 409 },
      );
    }
    return noCacheJson({ error: error.message }, { status: 500 });
  }

  const row = Array.isArray(data) ? data[0] : data;
  if (!row) {
    return noCacheJson(
      {
        error: "STRATEGY_VERSION_CONFLICT",
        message:
          "Configuration changed elsewhere. Reload the page and retry your edit.",
      },
      { status: 409 },
    );
  }

  return noCacheJson({ strategy: row });
}
