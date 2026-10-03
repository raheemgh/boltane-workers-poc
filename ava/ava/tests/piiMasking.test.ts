import { describe, expect, it } from "vitest";
import { maskPhoneNumbers, restoreInValue } from "../lib/piiMasking";

describe("maskPhoneNumbers", () => {
  it("masks a bare 9-digit-after-0 number and restores it exactly", () => {
    const { maskedText, restore } = maskPhoneNumbers("رقمي 0999123456 تواصل معي");
    expect(maskedText).toBe("رقمي [PHONE_1] تواصل معي");
    expect(restore(maskedText)).toBe("رقمي 0999123456 تواصل معي");
  });

  it("masks a +963-prefixed number with separators", () => {
    const { maskedText, restore } = maskPhoneNumbers("+963-999-123-456 هذا رقمي");
    expect(maskedText).toBe("[PHONE_1] هذا رقمي");
    expect(restore(maskedText)).toBe("+963-999-123-456 هذا رقمي");
  });

  it("masks multiple distinct numbers in one message with separate tokens", () => {
    const { maskedText, restore } = maskPhoneNumbers(
      "رقمي 0999111222 ورقم صاحبي 0988333444"
    );
    expect(maskedText).toBe("رقمي [PHONE_1] ورقم صاحبي [PHONE_2]");
    expect(restore(maskedText)).toBe("رقمي 0999111222 ورقم صاحبي 0988333444");
  });

  it("leaves text with no phone number untouched", () => {
    const { maskedText, restore } = maskPhoneNumbers("مرحبا، كيف حالك اليوم؟");
    expect(maskedText).toBe("مرحبا، كيف حالك اليوم؟");
    expect(restore(maskedText)).toBe("مرحبا، كيف حالك اليوم؟");
  });

  it("restore is a no-op on text with no placeholders", () => {
    const { restore } = maskPhoneNumbers("لا يوجد رقم هنا");
    expect(restore("رد عادي من النموذج بدون أي [PHONE_n]")).toBe(
      "رد عادي من النموذج بدون أي [PHONE_n]"
    );
  });
});

describe("restoreInValue", () => {
  it("restores a placeholder inside a flat object's string field", () => {
    const { restore } = maskPhoneNumbers("0999123456");
    const result = restoreInValue({ reply: "تواصل على [PHONE_1] رجاءً", done: false }, restore);
    expect(result).toEqual({ reply: "تواصل على 0999123456 رجاءً", done: false });
  });

  it("restores placeholders inside nested objects and arrays", () => {
    const { restore } = maskPhoneNumbers("0999123456");
    const result = restoreInValue(
      {
        extracted: { contact: { phone_note: "[PHONE_1]" } },
        tags: ["a", "[PHONE_1]", "b"],
      },
      restore
    );
    expect(result).toEqual({
      extracted: { contact: { phone_note: "0999123456" } },
      tags: ["a", "0999123456", "b"],
    });
  });

  it("leaves non-string values (booleans, numbers, null) untouched", () => {
    const { restore } = maskPhoneNumbers("0999123456");
    const result = restoreInValue({ done: true, count: 3, note: null }, restore);
    expect(result).toEqual({ done: true, count: 3, note: null });
  });
});
