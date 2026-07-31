import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:8010",
    channel: "msedge",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { ...devices["Pixel 5"], channel: "msedge" } },
  ],
  webServer: {
    command: "python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8010",
    cwd: "..",
    url: "http://127.0.0.1:8010/api/health",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
