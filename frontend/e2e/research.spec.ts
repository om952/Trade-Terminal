import { expect, test } from "@playwright/test";

test("research: plan variants, see the cost, approve, read the judged results", async ({ page }) => {
  const seeded = await page.request.post("/api/data/simulate", {
    data: { profile: "NIFTY", timeframe: "1d", days: 700, seed: 5 },
  });
  expect(seeded.ok()).toBeTruthy();

  await page.goto("/");
  await page.getByRole("tab", { name: "Builder" }).click();
  await page.locator(".field", { hasText: "Timeframe" }).locator("select").selectOption("1d");
  await page.locator(".field", { hasText: "Stored datasets" }).locator("select").selectOption("SIM|NIFTY");

  await page.getByRole("tab", { name: "Research" }).click();
  await page.getByLabel("What should the variants try?").fill("Fewer false signals");
  await page.getByRole("button", { name: "Plan research" }).click();

  const main = page.locator("main");
  const plan = main.getByLabel("Research plan");
  await expect(plan.getByText("RSI filter")).toBeVisible({ timeout: 60_000 });
  await expect(plan.getByText(/only changes numbers/)).toBeVisible();
  await expect(main.getByLabel("Cost")).toContainText("2 candidates");
  // Nothing was backtested before approval.
  const before = (await (await page.request.get("/api/runs?limit=500")).json()).runs.length;

  await main.getByRole("button", { name: "Approve and run" }).click();
  const results = main.getByLabel("Research results");
  await expect(results.getByText("the base strategy")).toBeVisible({ timeout: 120_000 });
  await expect(main.getByText(/survived after 2 tries/)).toBeVisible();
  const after = (await (await page.request.get("/api/runs?limit=500")).json()).runs.length;
  expect(after - before).toBeGreaterThanOrEqual(2);

  await main.getByRole("button", { name: /Finish|Keep none|Keep \d/ }).click();
  await expect(main.getByText("Done")).toBeVisible({ timeout: 60_000 });
});
