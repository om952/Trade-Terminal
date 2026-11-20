import { expect, test } from "@playwright/test";

test("portfolio: momentum rotation on a list, with a market filter, end to end", async ({ page }) => {
  for (const [profile, seed] of [["NIFTY", 31], ["RELIANCE", 32], ["HDFCBANK", 33], ["TCS", 34], ["INFY", 35]] as const) {
    const seeded = await page.request.post("/api/data/simulate", { data: { profile, timeframe: "1d", days: 600, seed } });
    expect(seeded.ok()).toBeTruthy();
  }

  await page.goto("/");
  await page.getByRole("tab", { name: "Screener" }).click();
  await page.getByRole("tab", { name: "Portfolio" }).click();
  await page.getByLabel("Starter portfolio").selectOption("momentum");
  await page.getByRole("button", { name: "My list" }).click();
  await page.getByLabel("Portfolio symbols").fill("SIM|RELIANCE, SIM|HDFCBANK, SIM|TCS, SIM|INFY");
  await page.getByLabel("Hold").fill("2");
  await page.getByLabel("Benchmark").fill("SIM|NIFTY");
  await expect(page.getByText("4 stocks")).toBeVisible();

  await page.getByRole("button", { name: "Backtest portfolio" }).click();
  const main = page.locator("main");
  await expect(main.getByLabel("Portfolio results")).toBeVisible({ timeout: 90_000 });
  await expect(main.getByText(/4 stocks · .* · filter on SIM\|NIFTY/)).toBeVisible();
  await expect(main.getByLabel("Is it luck?")).toBeVisible();
  await expect(main.getByLabel("Rebalances").locator("tbody tr").first()).toBeVisible();
  await expect(main.getByText(/Synthetic data/)).toBeVisible();

  // Paper trade it: a session starts today, so it waits for its first close (or, after the close
  // on a trading day, has recorded it).
  await main.getByRole("button", { name: "Paper trade this portfolio" }).click();
  const paper = main.getByLabel("Paper portfolio", { exact: true });
  await expect(paper).toBeVisible();
  await expect(main.getByLabel("Paper portfolio sessions").locator("tbody tr")).toHaveCount(1);
  await expect(paper.getByText(/Waiting for the first session close|Orders for the next open/).first()).toBeVisible({ timeout: 60_000 });
  await paper.getByRole("button", { name: "Update now" }).click();
  await expect(paper.getByText(/No new session close yet|Recorded \d+ new session/)).toBeVisible({ timeout: 60_000 });
  await paper.getByRole("button", { name: "Stop" }).click();
  await expect(main.getByLabel("Paper portfolio sessions").getByText("stopped")).toBeVisible();
  await expect(paper.getByRole("button", { name: "Update now" })).toBeDisabled();
});
