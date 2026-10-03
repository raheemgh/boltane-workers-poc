// tests/otpCode.test.ts
import { describe, it, expect } from "vitest";
import { OTP_CODE_LENGTH, generateOtpCode } from "../lib/otpCode";

describe("generateOtpCode", () => {
  it("produces a 12-digit numeric string, zero-padded", () => {
    for (let i = 0; i < 50; i++) {
      const code = generateOtpCode();
      expect(code).toHaveLength(OTP_CODE_LENGTH);
      expect(/^\d{12}$/.test(code)).toBe(true);
    }
  });

  it("isn't hardcoded to a single value across calls", () => {
    const codes = new Set(Array.from({ length: 20 }, () => generateOtpCode()));
    expect(codes.size).toBeGreaterThan(1);
  });
});
