import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { getSupabaseConfigIssues } from "@/lib/supabase/env";

export const dynamic = "force-dynamic";

export default async function Home() {
  if (getSupabaseConfigIssues().length > 0) {
    redirect("/login?error=config");
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  redirect(user ? "/portfolio" : "/login");
}
