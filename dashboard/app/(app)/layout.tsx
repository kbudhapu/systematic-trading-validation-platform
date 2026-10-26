import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { resolveEnvironment } from "@/lib/dashboard-data";
import { AppShell } from "@/components/AppShell";

export const dynamic = "force-dynamic";

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const supabase = await createClient();
  const {
    data: { user },
    error,
  } = await supabase.auth.getUser();

  if (error || !user) {
    redirect("/login?error=auth");
  }

  const environment = await resolveEnvironment(supabase);

  return (
    <AppShell environment={environment} userEmail={user.email ?? ""}>
      {children}
    </AppShell>
  );
}
