// tests/pdfScanSweep.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  findScanPendingSignups: vi.fn(),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
  deletePdfFromStorage: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../lib/malwareScan", async () => {
  const actual = await vi.importActual<typeof import("../lib/malwareScan")>(
    "../lib/malwareScan"
  );
  return {
    ...actual,
    getMalwareScanResult: vi.fn(),
  };
});

import { sweepPdfScans } from "../lib/pdfScanSweep";
import {
  deletePdfFromStorage,
  findScanPendingSignups,
  updatePendingSignup,
} from "../lib/supabase";
import { getMalwareScanResult, MalwareScanError } from "../lib/malwareScan";
import type { PendingSignupRow } from "../lib/types";

function makeRow(overrides: Partial<PendingSignupRow> = {}): PendingSignupRow {
  return {
    id: "row-1",
    phone_number_id: "109364823947271",
    store_name: "Sample Store",
    system_prompt: "You are Sami...",
    access_token: "meta-token",
    package: "basic",
    is_api_free: true,
    api_key: null,
    ai_model: null,
    status: "scan_pending",
    scan_id: "scan-abc",
    pdf_uploaded: true,
    pdf_storage_path: "row-1.pdf",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("sweepPdfScans", () => {
  it("clean verdict: resolves status back to otp_sent, keeps the stored file", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([makeRow()]);
    vi.mocked(getMalwareScanResult).mockResolvedValue({
      scanId: "scan-abc",
      status: "clean",
      threatStatus: "NO_THREATS_FOUND",
      fileName: "menu.pdf",
      threats: [],
    });

    const outcomes = await sweepPdfScans();

    expect(outcomes).toEqual([{ id: "row-1", result: "clean" }]);
    expect(deletePdfFromStorage).not.toHaveBeenCalled();
    expect(updatePendingSignup).toHaveBeenCalledWith("row-1", {
      status: "otp_sent",
      pdf_scan_status: "clean",
      scan_id: null,
    });
  });

  it("infected verdict: deletes the stored file and clears pdf fields", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([makeRow()]);
    vi.mocked(getMalwareScanResult).mockResolvedValue({
      scanId: "scan-abc",
      status: "infected",
      threatStatus: "THREATS_FOUND",
      fileName: "menu.pdf",
      threats: ["eicar-test"],
    });

    const outcomes = await sweepPdfScans();

    expect(outcomes).toEqual([{ id: "row-1", result: "infected" }]);
    expect(deletePdfFromStorage).toHaveBeenCalledWith("row-1.pdf");
    expect(updatePendingSignup).toHaveBeenCalledWith("row-1", {
      status: "otp_sent",
      pdf_uploaded: false,
      pdf_storage_path: null,
      pdf_scan_status: "infected",
      scan_id: null,
    });
  });

  it("still-running verdict: leaves the row untouched", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([makeRow()]);
    vi.mocked(getMalwareScanResult).mockResolvedValue({
      scanId: "scan-abc",
      status: "clean",
      threatStatus: "RUNNING",
      fileName: "menu.pdf",
      threats: [],
    });

    const outcomes = await sweepPdfScans();

    expect(outcomes).toEqual([{ id: "row-1", result: "still-running" }]);
    expect(updatePendingSignup).not.toHaveBeenCalled();
    expect(deletePdfFromStorage).not.toHaveBeenCalled();
  });

  it("a row with no scan_id is reported as an error and doesn't block the rest of the sweep", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([
      makeRow({ id: "row-bad", scan_id: null }),
      makeRow({ id: "row-2" }),
    ]);
    vi.mocked(getMalwareScanResult).mockResolvedValue({
      scanId: "scan-abc",
      status: "clean",
      threatStatus: "NO_THREATS_FOUND",
      fileName: "menu.pdf",
      threats: [],
    });

    const outcomes = await sweepPdfScans();

    expect(outcomes).toEqual([
      { id: "row-bad", result: "error", error: "scan_pending row missing scan_id" },
      { id: "row-2", result: "clean" },
    ]);
  });

  it("a sed.sh error for one row is captured, not thrown", async () => {
    vi.mocked(findScanPendingSignups).mockResolvedValue([makeRow()]);
    vi.mocked(getMalwareScanResult).mockRejectedValue(
      new MalwareScanError("sed.sh /malware/scan/scan-abc failed (500): oops")
    );

    const outcomes = await sweepPdfScans();

    expect(outcomes).toEqual([
      {
        id: "row-1",
        result: "error",
        error: "sed.sh /malware/scan/scan-abc failed (500): oops",
      },
    ]);
    expect(updatePendingSignup).not.toHaveBeenCalled();
  });
});
