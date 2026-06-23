import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Lets imports look like `import { Button } from '@/components/ui/button'`
      // matching the shadcn convention and keeping deep relative paths out of view.
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // dev-server proxy so the React app can call `/projects` directly
    // and Vite forwards to FastAPI on :8000. Avoids CORS friction in dev.
    // Production deployments serve via Caddy reverse proxy (see Caddyfile).
    proxy: {
      "/projects": "http://localhost:8000",
      "/reviews": "http://localhost:8000",
      "/branch-events": "http://localhost:8000",
      "/repos": "http://localhost:8000",
    },
  },
});
