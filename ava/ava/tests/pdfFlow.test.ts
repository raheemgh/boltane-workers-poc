// tests/pdfFlow.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  uploadPdfToStorage: vi.fn().mockResolvedValue(undefined),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
}));

import { buildPdfStoragePath, storePdfAndOverrideStatus } from "../lib/pdfFlow";
import { updatePendingSignup, uploadPdfToStorage } from "../lib/supabase";
import type { PendingSignupRow } from "../lib/types";

beforeEach(() => {
  vi.clearAllMocks();
});

const pending: PendingSignupRow = {
  id: "row-42",
  phone_number_id: "109364823947271",
  store_name: "Sample Store",
  system_prompt: "You are Sami...",
  access_token: "meta-token",
  package: "basic",
  is_api_free: true,
  api_key: null,
  ai_model: null,
  status: "otp_sent",
  otp_code: "123456789012",
  otp_sent_at: "2026-01-01T00:00:00.000Z",
};

describe("buildPdfStoragePath", () => {
  it("is deterministic per pending_signups id", () => {
    expect(buildPdfStoragePath("row-42")).toBe("row-42.pdf");
  });
});

describe("storePdfAndOverrideStatus", () => {
  it("uploads the file to the deterministic path", async () => {
    const buffer = Buffer.from("pdf bytes");

    const result = await storePdfAndOverrideStatus(pending, buffer);

    expect(uploadPdfToStorage).toHaveBeenCalledWith("row-42.pdf", buffer);
    expect(result.storagePath).toBe("row-42.pdf");
  });

  it("marks pdf_uploaded + storage path, without touching status (manual OTP has no interim status anymore)", async () => {
    const buffer = Buffer.from("pdf bytes");

    await storePdfAndOverrideStatus(pending, buffer);

    expect(updatePendingSignup).toHaveBeenCalledWith("row-42", {
      pdf_uploaded: true,
      pdf_storage_path: "row-42.pdf",
    });
  });

  it("does not include a status field in the patch at all", async () => {
    const buffer = Buffer.from("pdf bytes");
    await storePdfAndOverrideStatus(pending, buffer);

    const patch = vi.mocked(updatePendingSignup).mock.calls[0][1];
    expect(patch).not.toHaveProperty("status");
    expect(patch).not.toHaveProperty("activate_at");
  });
});
