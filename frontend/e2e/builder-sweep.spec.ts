import { expect, test } from "@playwright/test";

test("builder: vary a parameter, run the sweep, open a variant without losing the sweep", async ({ page }) => {
  const seeded = await page.request.post("/api/data/simulate", {
    data: { profile: "NIFTY", timeframe: "15m", days: 300, seed: 7 },
  });
  expect(seeded.ok()).toBeTruthy();
  const before = (await (await page.request.get("/api/runs?limit=500")).json()).runs.length;

  await page.goto("/");
  await page.getByRole("tab", { name: "Builder" }).click();
  await page.locator(".field", { hasText: "Timeframe" }).locator("select").selectOption("15m");
  await page.locator(".field", { hasText: "Stored datasets" }).locator("select").selectOption("SIM|NIFTY");

  await page.getByLabel("Add a parameter to sweep").selectOption("/indicators/0/params/period");
  await page.getByLabel("Values for ema_fast.period").fill("5, 9");
  const run = page.getByRole("button", { name: "Run sweep (2 backtests)" });
  await expect(run).toBeEnabled();     // the backend validated the swept document
  await run.click();

  await expect(page.getByRole("button", { name: "Sweep (2)" })).toBeVisible({ timeout: 60_000 });
  const rows = page.locator("table tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).locator("td").first()).toHaveText("5");
  await expect(rows.nth(1).locator("td").first()).toHaveText("9");
  await expect(page.locator("main").getByText(/partly luck/)).toBeVisible();

  const after = (await (await page.request.get("/api/runs?limit=500")).json()).runs.length;
  expect(after - before).toBe(2);       // every variant is a recorded run

  await rows.nth(1).getByRole("button", { name: "Report" }).click();
  await expect(page.getByRole("button", { name: "Performance" })).toHaveClass(/active/);
  await expect(page.getByRole("button", { name: "Run sweep (2 backtests)" })).toBeVisible();
});
