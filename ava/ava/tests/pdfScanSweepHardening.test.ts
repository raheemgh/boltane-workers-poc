// tests/pdfScanSweepHardening.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  findScanPendingSignups: vi.fn(),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
  deletePdfFromStorage: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../lib/malwareScan", async () => {
  const actual = await vi.importActual<typeof import("../lib/malwareScan")>("../lib/malwareScan");
  return { ...actual, getMalwareScanResult: vi.fn() };
});

import { sweepPdfScans } from "../lib/pdfScanSweep";
import { deletePdfFromStorage, findScanPendingSignups, updatePendingSignup } from "../lib/supabase";
import { getMalwareScanResult } from "../lib/malwareScan";

const row = (id: string) =>
  ({ id, scan_id: `scan-${id}`, pdf_storage_path: `${id}.pdf`, status: "scan_pending" }) as never;
const verdict = (over: Record<string, unknown>) =>
  ({ scanId: "s", fileName: "f.pdf", threats: [], status: "clean", threatStatus: "NO_THREATS_FOUND", ...over }) as never;

beforeEach(() => vi.clearAllMocks());

describe("sweep isolation", () => {
  it("a storage-delete failure on one row does not stop the next row", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([row("bad"), row("good")]);
    vi.mocked(getMalwareScanResult)
      .mockResolvedValueOnce(verdict({ status: "infected", threatStatus: "THREATS_FOUND" }))
      .mockResolvedValueOnce(verdict({}));
    vi.mocked(deletePdfFromStorage).mockRejectedValueOnce(new Error("storage 500"));

    const out = await sweepPdfScans();

    expect(out.map((o) => [o.id, o.result])).toEqual([["bad", "error"], ["good", "clean"]]);
    // the failed row was NOT touched, so it is retried next pass
    expect(vi.mocked(updatePendingSignup).mock.calls.map((c) => c[0])).toEqual(["good"]);
  });
});

describe("sweep fails closed", () => {
  it("THREATS_FOUND is infected even if status isn't literally 'infected'", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([row("a")]);
    vi.mocked(getMalwareScanResult).mockResolvedValue(verdict({ status: "skipped", threatStatus: "THREATS_FOUND" }));
    const [o] = await sweepPdfScans();
    expect(o.result).toBe("infected");
    expect(deletePdfFromStorage).toHaveBeenCalledWith("a.pdf");
  });

  it("an unrecognized verdict is NOT marked clean; row is left untouched", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([row("a")]);
    vi.mocked(getMalwareScanResult).mockResolvedValue(verdict({ status: "skipped", threatStatus: "NO_THREATS_FOUND" }));
    const [o] = await sweepPdfScans();
    expect(o.result).toBe("error");
    expect(updatePendingSignup).not.toHaveBeenCalled();
  });

  it("explicit clean still resolves to clean (unchanged behaviour)", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([row("a")]);
    vi.mocked(getMalwareScanResult).mockResolvedValue(verdict({}));
    const [o] = await sweepPdfScans();
    expect(o.result).toBe("clean");
    expect(updatePendingSignup).toHaveBeenCalledWith("a", { status: "otp_sent", pdf_scan_status: "clean", scan_id: null });
  });
});
