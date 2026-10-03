// lib/sanitize.ts
//
// Runtime shape checks for the client-controlled parts of POST /chat.
// The TypeScript types (ConversationMessage, BusinessContext) are not
// enforced at runtime, and a malformed body used to throw outside any
// try/catch. These helpers only drop/ignore values that could not have
// been valid anyway — a well-formed request passes through unchanged.
import type { BusinessContext, ConversationMessage } from "./types";

export function sanitizeConversationHistory(raw: unknown): ConversationMessage[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (m): m is ConversationMessage =>
      !!m &&
      typeof m === "object" &&
      ((m as { role?: unknown }).role === "user" ||
        (m as { role?: unknown }).role === "assistant") &&
      typeof (m as { content?: unknown }).content === "string"
  );
}

const STRING_KEYS = [
  "project_name",
  "contact_number",
  "domain",
  "business_nature",
  "country",
] as const;

export function sanitizeBusinessContext(raw: unknown): BusinessContext | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const out: Record<string, unknown> = { ...(raw as Record<string, unknown>) };
  for (const key of STRING_KEYS) {
    const v = out[key];
    if (v !== undefined && v !== null && typeof v !== "string") delete out[key];
  }
  return out as BusinessContext;
}
