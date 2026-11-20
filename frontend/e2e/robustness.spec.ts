import { expect, test } from "@playwright/test";

test("robustness: backtest in the builder, then walk-forward and slippage checks", async ({ page }) => {
  const seeded = await page.request.post("/api/data/simulate", {
    data: { profile: "NIFTY", timeframe: "15m", days: 300, seed: 7 },
  });
  expect(seeded.ok()).toBeTruthy();

  await page.goto("/");
  await page.getByRole("tab", { name: "Builder" }).click();
  await page.locator(".field", { hasText: "Timeframe" }).locator("select").selectOption("15m");
  await page.locator(".field", { hasText: "Stored datasets" }).locator("select").selectOption("SIM|NIFTY");
  await page.getByRole("button", { name: "Run backtest" }).click();

  const main = page.locator("main");
  await expect(main.getByLabel("Is it luck?")).toBeVisible({ timeout: 60_000 });
  await main.getByLabel("Walk-forward folds").fill("2");
  await main.getByRole("button", { name: "Run checks" }).click();

  await expect(page.getByRole("button", { name: "Robustness" })).toHaveClass(/active/);
  const verdicts = main.getByLabel("Verdicts");
  await expect(verdicts.getByText(/Profitable in \d of 2 walk-forward folds/)).toBeVisible({ timeout: 90_000 });
  await expect(main.getByText("Slippage stress")).toBeVisible();
  await expect(main.locator("table").first().locator("tbody tr")).toHaveCount(2);
  await expect(main.getByText(/do not count as trials/)).toBeVisible();
});
