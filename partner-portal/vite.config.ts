import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: forward the Consent Manager API to a local backend so the portal
// can run without CORS (same as the CM console). The backend serves everything
// under /consent/v1, so the prefix is proxied verbatim. In production the
// portal is served by nginx and the API is reached via config.json apiBaseUrl
// + Istio routing. The composite (compositeUrl) is called directly.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/consent": {
        target: process.env.CM_API_URL || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
