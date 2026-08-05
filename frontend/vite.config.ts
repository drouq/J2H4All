import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API traffic to the FastAPI backend on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/auth": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
});
