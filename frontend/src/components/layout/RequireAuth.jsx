import { Navigate, Outlet, useLocation } from "react-router-dom";
import { Loader2 } from "lucide-react";

import { useAuth } from "@/lib/auth";

/**
 * Route guard used as a wrapper around the protected `<Shell />` outlet.
 *
 *   • While the initial /auth/me is still in flight → show a spinner
 *     so we don't flash the login page every reload.
 *   • Once we know we're unauthenticated → redirect to /login, remembering
 *     where the user was going so we can bounce them back after login.
 *   • Once authenticated → render the child outlet (Shell + pages).
 */
export default function RequireAuth() {
  const auth = useAuth();
  const location = useLocation();

  if (auth.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
      </div>
    );
  }

  if (!auth.isAuthed) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <Outlet />;
}
