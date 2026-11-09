import { defineConfig } from "@playwright/test";
import { resolve } from "path";

/**
 * E2E（模块验收）：并发 1（后端 SQLite 单写者 + LLM 限流友好）；
 * webServer 自起 api(8100) + web(3000)，复用已有进程（reuseExistingServer）。
 */
const API_DIR = resolve(__dirname, "../api");

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  workers: 1,
  retries: 0,
  use: { baseURL: "http://localhost:3000", trace: "off" },
  webServer: [
    {
      command: `"${resolve(API_DIR, ".venv/Scripts/python.exe")}" -m uvicorn app.main:app --port 8100`,
      cwd: API_DIR,
      url: "http://localhost:8100/api/health",
      reuseExistingServer: true,
      timeout: 90_000,
    },
    {
      command: "pnpm dev",
      url: "http://localhost:3000",
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
