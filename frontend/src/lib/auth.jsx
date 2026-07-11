import { createContext, useContext, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api";

/**
 * Auth context — reads from `GET /auth/me` on mount, refetches on
 * `refetchOnWindowFocus` so a session that expired while the tab was
 * backgrounded transitions the UI back to the login screen on refocus.
 *
 * `signIn(password)` and `signOut()` are mutation-driven so error state
 * (401 on wrong password) surfaces naturally to the login form.
 */

const AuthContext = createContext(null);
const AUTH_QUERY_KEY = ["auth", "me"];

export function AuthProvider({ children }) {
  const queryClient = useQueryClient();

  const meQuery = useQuery({
    queryKey: AUTH_QUERY_KEY,
    queryFn: () => api.authMe(),
    // On any error other than 401, retry once. 401 is a state, not a fault.
    retry: (failureCount, err) =>
      failureCount < 1 && !(err instanceof ApiError && err.status === 401),
    staleTime: 60_000,
  });

  const loginMutation = useMutation({
    mutationFn: (password) => api.authLogin(password),
    onSuccess: (data) => {
      // Prime the me query so downstream consumers don't render "loading"
      // while a refetch races the redirect.
      queryClient.setQueryData(AUTH_QUERY_KEY, data);
      // Clear any cached failed-under-401 queries so protected pages refetch.
      queryClient.invalidateQueries({ predicate: () => true });
    },
  });

  const logoutMutation = useMutation({
    mutationFn: () => api.authLogout(),
    onSettled: () => {
      // Wipe every cached query — sensitive data belongs to the session
      // that just ended.
      queryClient.clear();
    },
  });

  const value = useMemo(
    () => ({
      /** True while the initial /auth/me is still resolving. */
      isLoading: meQuery.isLoading,
      /** True if the server says the session cookie is valid. */
      isAuthed: !!meQuery.data?.authenticated,
      /** True if the backend has DASHBOARD_PASSWORD set at all. */
      isConfigured: !!meQuery.data?.configured,
      /** Sign in. Returns the mutation for pending / error inspection. */
      signIn: loginMutation,
      /** Sign out — always succeeds server-side. */
      signOut: logoutMutation,
    }),
    [meQuery.isLoading, meQuery.data, loginMutation, logoutMutation]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return ctx;
}
