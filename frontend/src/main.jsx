import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import App from "./App";
import { AuthProvider } from "@/lib/auth";
import { ThemeProvider } from "@/lib/theme";
import "./index.css";

/**
 * One QueryClient for the whole app. Defaults are chosen for a dashboard
 * (read-heavy, occasional polling):
 *
 *   • `staleTime: 30s` — most data the dashboard shows changes on the order
 *     of minutes. Avoids hammering the API on every component re-render.
 *   • `refetchOnWindowFocus: true` (the default) — when the user tabs back to
 *     the dashboard, freshen project + branch-events counts so the red dot
 *     reflects reality.
 *   • Errors surface via `useQuery().isError` / `error`. We do NOT install a
 *     global error boundary for queries — each page handles its own state
 *     so the layout stays usable when one card fails.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
    },
  },
});

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ThemeProvider defaultTheme="system">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          {/* AuthProvider lives INSIDE the router so it can eventually
              read location for its own guards, and INSIDE QueryClient so
              it can use useMutation for login/logout without instantiating
              its own client. */}
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>
);
