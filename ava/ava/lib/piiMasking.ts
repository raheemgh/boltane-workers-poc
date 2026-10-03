// lib/piiMasking.ts
//
// v1 scope: phone numbers only. Names are deliberately NOT masked yet —
// reliably telling an Arabic given name apart from an ordinary word
// needs either a real NER model or a maintained name dictionary, and a
// naive regex would both miss real names and flag ordinary words,
// corrupting conversation context either way. Phone numbers are the
// safe, high-confidence part to ship first; this file is written so a
// second masker (names) can be added alongside it later without
// touching call sites.
//
// Deliberately NOT transliterating to ASCII/Latin letters first. That
// step doesn't help: Node's regex engine already handles Arabic Unicode
// text natively, so there's nothing to gain there, and the hard part of
// PII masking was never "the text isn't ASCII" — it's telling PII apart
// from ordinary words, which a letter-for-letter remapping doesn't
// help with at all. It would also make restoring the exact original
// text less reliable, not more: several distinct Arabic letters
// commonly map to the same Latin letter in transliteration schemes, so
// the mapping loses information it can't get back.

export interface MaskResult {
  maskedText: string;
  /** Replaces every [PHONE_n] placeholder in `text` with the original
   *  number it stood for. Safe to call on text that contains no
   *  placeholders at all — it's a no-op then. */
  restore: (text: string) => string;
}

// Covers the common written forms of a Syrian/regional mobile number:
// optional +963 / 00963 / a leading 0, then a 9-something subscriber
// number, with optional spaces/dots/dashes between digit groups (e.g.
// "0999 123 456", "+963-999-123-456", "0999123456"). Also matches a
// bare 9-digit run with no separators at all, which covers most
// customer messages in practice.
const PHONE_PATTERN =
  /(?:\+?963|0{1,2}963|0)?[\s.-]?9\d{2}[\s.-]?\d{3}[\s.-]?\d{3}\b/g;

/**
 * One masker for a WHOLE request (system prompt + every message).
 * Placeholder numbers are unique across everything it masks, and the
 * same original number always gets the same placeholder. Numbering
 * per-message (the old behaviour) made two different numbers in two
 * messages both become [PHONE_1], and the model's [PHONE_1] was then
 * restored to the wrong number.
 */
export interface PhoneMasker {
  mask: (text: string) => string;
  restore: (text: string) => string;
}

export function createPhoneMasker(): PhoneMasker {
  const found: string[] = [];

  const mask = (text: string): string =>
    text.replace(PHONE_PATTERN, (match) => {
      let idx = found.indexOf(match);
      if (idx === -1) {
        found.push(match);
        idx = found.length - 1;
      }
      return `[PHONE_${idx + 1}]`;
    });

  const restore = (responseText: string): string =>
    found.reduce(
      (acc, original, i) => acc.split(`[PHONE_${i + 1}]`).join(original),
      responseText
    );

  return { mask, restore };
}

export function maskPhoneNumbers(text: string): MaskResult {
  const masker = createPhoneMasker();
  const maskedText = masker.mask(text);
  return { maskedText, restore: masker.restore };
}

/**
 * Walks a parsed JSON value (from the model's response) and applies
 * `restore` to every string it finds, recursively — covers both a flat
 * shape ({ reply, done }) and a nested one (extraction results) without
 * either call site needing to know the other's shape.
 */
export function restoreInValue<T>(value: T, restore: (text: string) => string): T {
  if (typeof value === "string") {
    return restore(value) as unknown as T;
  }
  if (Array.isArray(value)) {
    return value.map((item) => restoreInValue(item, restore)) as unknown as T;
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, val] of Object.entries(value as Record<string, unknown>)) {
      out[key] = restoreInValue(val, restore);
    }
    return out as T;
  }
  return value;
}
