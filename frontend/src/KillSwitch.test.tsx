import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type KillSwitchReport, type KillSwitchState } from "./api";
import { KillSwitchBanner, KillSwitchButton, useKillSwitch } from "./KillSwitch";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const OFF: KillSwitchState = { engaged: false, engaged_at: null, reason: null, released_at: null };
const REPORT: KillSwitchReport = {
  engaged: true, engaged_at: "2026-11-21T10:15:00+05:30", reason: "drill", released_at: null,
  sessions: [{ session_id: "s1", strategy: "EMA", symbol: "SIM|X", positions_closed: 2, still_open: [], stopped: true }],
  portfolios: [{ id: "pfs_1", name: "Rotation" }], at: "2026-11-21T10:15:01+05:30", complete: true,
};

function Harness() {
  const kill = useKillSwitch(() => undefined);
  return <><KillSwitchButton kill={kill} /><KillSwitchBanner kill={kill} /></>;
}

describe("Kill switch", () => {
  it("needs FLATTEN typed before it engages, then shows what it did", async () => {
    vi.spyOn(api, "killSwitch").mockResolvedValue(OFF);
    const engage = vi.spyOn(api, "engageKillSwitch").mockResolvedValue(REPORT);
    render(<Harness />);
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Flatten all" }));
    const go = screen.getByRole("button", { name: "Flatten everything" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Type FLATTEN to confirm"), { target: { value: "flatten" } });
    expect(go.disabled).toBe(true);                          // the exact word, in capitals
    fireEvent.change(screen.getByLabelText("Reason (optional)"), { target: { value: "drill" } });
    fireEvent.change(screen.getByLabelText("Type FLATTEN to confirm"), { target: { value: "FLATTEN" } });
    expect(go.disabled).toBe(false);
    fireEvent.click(go);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(engage).toHaveBeenCalledWith("drill");
    expect(screen.queryByRole("dialog")).toBeNull();
    const banner = screen.getByRole("alert").textContent ?? "";
    expect(banner).toContain("no new sessions can start");
    expect(banner).toContain("1 paper session stopped, 2 positions closed, 1 paper portfolio stopped");
  });

  it("shows an engaged switch on load, and releases only after a second confirmation", async () => {
    vi.spyOn(api, "killSwitch").mockResolvedValue({ ...OFF, engaged: true, engaged_at: "2026-11-21T10:15:00+05:30", reason: "drill" });
    const release = vi.spyOn(api, "releaseKillSwitch").mockResolvedValue({ ...OFF, released_at: "2026-11-21T11:00:00+05:30" });
    render(<Harness />);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("(drill)"));

    fireEvent.click(screen.getByRole("button", { name: "Release…" }));
    expect(release).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Release" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(release).toHaveBeenCalledTimes(1);
  });

  it("cancel closes the confirmation without engaging", () => {
    vi.spyOn(api, "killSwitch").mockResolvedValue(OFF);
    const engage = vi.spyOn(api, "engageKillSwitch");
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Flatten all" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(engage).not.toHaveBeenCalled();
  });
});
