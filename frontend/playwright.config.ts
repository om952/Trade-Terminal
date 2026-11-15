// End-to-end: the real UI, the real API, graph and engine, on a throwaway data
// directory. Only the model is scripted (e2e/script.json), so the run is free,
// offline and deterministic.
import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

const API_PORT = 8766;
const UI_PORT = 5174;
const dataDir = process.env.E2E_DATA_DIR ?? mkdtempSync(join(tmpdir(), "tt-e2e-"));
process.env.E2E_DATA_DIR = dataDir;   // shared with the worker processes
const root = resolve(here, "..");

// E2E_BASE_URL points the same test at an already running app, such as an
// installed build serving its own UI; no servers are started then.
const external = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  retries: 0,
  use: { baseURL: external ?? `http://127.0.0.1:${UI_PORT}`, trace: "retain-on-failure" },
  webServer: external ? undefined : [
    {
      command: `"${root}/.venv/bin/uvicorn" backend.api.main:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: root,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: false,
      env: {
        DATA_DIR: dataDir,
        LLM_PROVIDER: "scripted",
        LLM_SCRIPT: resolve(here, "e2e/script.json"),
        API_ALLOWED_ORIGINS: `http://127.0.0.1:${UI_PORT}`,
        TELEGRAM_BOT_TOKEN: "",
        TELEGRAM_CHAT_ID: "",
        UPSTOX_ACCESS_TOKEN: "",
      },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${UI_PORT} --strictPort`,
      url: `http://127.0.0.1:${UI_PORT}`,
      reuseExistingServer: false,
      env: { API_TARGET: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
