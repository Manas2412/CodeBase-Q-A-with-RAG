import { useEffect, useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router-dom";
import { AlertCircle, KeyRound, Loader2, Terminal } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Password-gate. Redirects to `from` (or /projects) after a successful login.
 *
 * If the backend has no `DASHBOARD_PASSWORD` configured, we surface that as
 * a bright warning — the operator needs to set it in backend/.env or the
 * app can never be unlocked. This is more helpful than the generic
 * "Invalid password" error the user would otherwise see forever.
 */
export default function Login() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState("");

  const from = location.state?.from?.pathname || "/projects";

  useEffect(() => {
    if (auth.isAuthed) navigate(from, { replace: true });
  }, [auth.isAuthed, from, navigate]);

  // Hide the form entirely once logged in — prevents a flash of the login
  // page while the redirect useEffect processes.
  if (auth.isAuthed) return <Navigate to={from} replace />;

  const submitting = auth.signIn.isPending;
  const wrongPw =
    auth.signIn.isError &&
    auth.signIn.error instanceof ApiError &&
    auth.signIn.error.status === 401;

  return (
    <div className="mx-auto flex min-h-[calc(100vh-4rem)] max-w-md flex-col items-center justify-center gap-6 px-4">
      <div className="flex items-center gap-2 text-lg font-semibold">
        <Terminal className="size-5 text-primary" />
        Code Review Agent
      </div>

      <Card className="w-full">
        <CardHeader className="space-y-1">
          <CardTitle className="text-base">Sign in</CardTitle>
          <CardDescription className="text-xs">
            This dashboard is internal — enter the shared password to continue.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {auth.isConfigured === false ? (
            <Alert variant="warning" className="mb-4">
              <AlertCircle className="size-4" />
              <AlertTitle>Backend password not configured</AlertTitle>
              <AlertDescription className="mt-1">
                Set <code className="font-mono">DASHBOARD_PASSWORD</code> in
                <code className="font-mono"> backend/.env</code>, then restart the FastAPI server.
              </AlertDescription>
            </Alert>
          ) : null}

          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!password || submitting) return;
              auth.signIn.mutate(password);
            }}
            className="space-y-3"
          >
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoFocus
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
              />
            </div>

            {wrongPw ? (
              <Alert variant="destructive">
                <AlertCircle className="size-4" />
                <AlertDescription>Invalid password.</AlertDescription>
              </Alert>
            ) : null}

            <Button
              type="submit"
              disabled={!password || submitting}
              className="w-full"
            >
              {submitting ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  Signing in…
                </>
              ) : (
                <>
                  <KeyRound className="size-4" />
                  Sign in
                </>
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
