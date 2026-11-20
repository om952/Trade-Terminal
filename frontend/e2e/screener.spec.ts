import { expect, test } from "@playwright/test";

test("screener: scan a list now and in history, with unscannable stocks reported", async ({ page }) => {
  for (const [profile, seed] of [["NIFTY", 3], ["RELIANCE", 4]] as const) {
    const seeded = await page.request.post("/api/data/simulate", {
      data: { profile, timeframe: "1d", days: 600, seed },
    });
    expect(seeded.ok()).toBeTruthy();
  }

  await page.goto("/");
  await page.getByRole("tab", { name: "Screener" }).click();
  await page.getByLabel("Starter scan").selectOption("above_sma50");
  await page.getByRole("button", { name: "My list" }).click();
  await page.getByLabel("Symbols to scan").fill("SIM|NIFTY, SIM|RELIANCE, SIM|NOTHING");
  await expect(page.getByText("3 stocks")).toBeVisible();

  await page.getByRole("button", { name: "Scan now" }).click();
  const main = page.locator("main");
  await expect(main.getByText(/Above 50-day SMA · as of/)).toBeVisible({ timeout: 60_000 });
  const rows = main.locator("table tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(main.getByText("1 not scanned")).toBeVisible();
  // Every scanned row says where price is relative to the line.
  for (const text of await rows.locator("td:last-child").allTextContents()) {
    expect(text).toMatch(/% (beyond|to go)$/);
  }

  await page.getByRole("button", { name: "Scan history" }).click();
  await expect(main.getByText(/Above 50-day SMA · history/)).toBeVisible({ timeout: 60_000 });
  await expect(main.getByText(/baseline/).first()).toBeVisible();
  await expect(main.getByText(/differ from simply holding/)).toBeVisible();

  // Save it, switch the daily alert on, and check now: a first check records a
  // baseline; a second on the same bars finds no new session.
  const aside = page.locator("aside");
  await aside.getByRole("button", { name: "Save", exact: true }).click();
  await expect(aside.getByText("Saved", { exact: true })).toBeVisible();
  await aside.getByLabel("Daily alert").check();
  await expect(aside.getByText("Daily alert on")).toBeVisible();
  await expect(aside.getByText(/Telegram is not set up/)).toBeVisible();
  await expect(page.getByLabel("Saved scan")).toContainText("Above 50-day SMA · alert");

  await aside.getByRole("button", { name: "Check now" }).click();
  await expect(aside.getByText(/Baseline recorded: \d of 2 meet the condition/)).toBeVisible({ timeout: 60_000 });
  await aside.getByRole("button", { name: "Check now" }).click();
  await expect(aside.getByText(/No new session since the last check/)).toBeVisible({ timeout: 60_000 });
});
