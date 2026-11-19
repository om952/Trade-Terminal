import { expect, test } from "@playwright/test";

test("describe, approve, backtest, open the report, paper trade, find it in the library", async ({ page }) => {
  // Synthetic data, so nothing depends on a broker connection.
  const seeded = await page.request.post("/api/data/simulate", {
    data: { profile: "NIFTY", timeframe: "15m", days: 300, seed: 7 },
  });
  expect(seeded.ok()).toBeTruthy();
  // Other specs share this data directory, so count runs from here.
  const runs = async () => (await (await page.request.get("/api/runs?limit=500")).json()).runs.length;
  const runsAtStart = await runs();

  await page.goto("/");
  await expect(page.getByText("Strategy assistant")).toBeVisible();
  await expect(page.getByText("scripted:script.json")).toBeVisible();

  await page.getByLabel("Describe a strategy").fill(
    "On SIM|NIFTY 15-minute, buy when the 9 EMA crosses above the 21 EMA, exit on the cross back.",
  );
  await page.getByRole("button", { name: "Build strategy" }).click();

  // The read-back is the approval gate; nothing has run yet.
  await expect(page.getByText(/Check before anything runs/)).toBeVisible();
  await expect(page.getByText("15-minute bars", { exact: true })).toBeVisible();
  expect(await runs()).toBe(runsAtStart);

  await page.getByRole("button", { name: "Approve and backtest" }).click();
  await expect(page.getByRole("button", { name: "Open full report" })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("synthetic data", { exact: true })).toBeVisible();
  await expect(page.getByText(/check of the mechanics/)).toBeVisible();

  await page.getByRole("button", { name: "Open full report" }).click();
  await expect(page.getByRole("button", { name: /Trade Log \(\d+\)/ })).toBeEnabled();

  await page.getByRole("button", { name: "Start paper trading" }).click();
  await expect(page.getByRole("button", { name: "Live" })).toHaveClass(/active/);

  await page.getByRole("tab", { name: "Library" }).click();
  await page.getByRole("button", { name: /EMA 9\/21 crossover \(e2e\) v1 · 1 run/ }).click();
  await expect(page.getByText(/v1 · agent/)).toBeVisible();
});
