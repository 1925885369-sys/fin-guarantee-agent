import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  if (command === "build" && env.VITE_USE_PROFILE_MOCKS === "true") {
    throw new Error("Production builds cannot enable VITE_USE_PROFILE_MOCKS.");
  }
  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: true,
      proxy: { "/api": env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000" },
    },
    build: { outDir: "dist" },
  };
});
