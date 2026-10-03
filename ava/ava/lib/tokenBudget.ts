// lib/tokenBudget.ts
//
// Enforces Ava's per-call and per-attempt OUTPUT token budgets. There
// is deliberately no equivalent cap on INPUT tokens anywhere in this
// codebase — see README's "Token budgets" section for why (in short:
// truncating what the model can read risks losing onboarding data
// mid-conversation; capping what it can GENERATE doesn't).
//
// REPLY_MAX_OUTPUT_TOKENS / EXTRACTION_MAX_OUTPUT_TOKENS are per-call
// caps, enforced by passing maxOutputTokens straight through to
// OpenRouter's own max_tokens parameter (lib/openrouter.ts) — that's a
// hard, exact enforcement, no estimation involved.
//
// CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT is different: it has to hold
// across MULTIPLE requests in a fully stateless API (see api/chat.ts's
// header comment — this app has no session memory of its own beyond
// what contact_number tracking opts into, and even that isn't
// guaranteed to be present). The only state available to reconstruct
// "how much has this attempt already spent" from is whatever
// conversation_history the frontend resends each turn. So this is
// enforced via ESTIMATION, not exact accounting: estimateTokens() is a
// simple chars/4 heuristic (a common rule-of-thumb approximation when
// a real tokenizer isn't available), summed across every past
// assistant-role message. This is a soft budget guard against a
// conversation running unexpectedly long, not a precise cost ledger —
// flagging that plainly rather than implying more precision than it
// has.
import type { ConversationMessage } from "./types";

export const REPLY_MAX_OUTPUT_TOKENS = 200; // within the specified 150-200 range
export const EXTRACTION_MAX_OUTPUT_TOKENS = 1200; // within the specified 1000-1200 range
export const CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT = 5000;

export function estimateTokens(text: string): number {
  // Defensive: a non-string can never be a valid message body (and used
  // to throw / produce NaN, which silently bypassed the budget check).
  if (typeof text !== "string") return 0;
  return Math.ceil(text.length / 4);
}

/**
 * Sums estimated output tokens across every assistant-role message
 * already in the transcript — i.e. tokens already "spent" generating
 * prior replies this attempt. User messages are deliberately excluded
 * (no input cap — see header comment above).
 */
export function estimateAssistantTokensSoFar(
  messages: ConversationMessage[]
): number {
  return messages
    .filter((m) => m.role === "assistant")
    .reduce((sum, m) => sum + estimateTokens(m.content), 0);
}

/**
 * true if making a call capped at plannedCallTokens would push this
 * attempt's estimated cumulative output past the budget.
 */
export function wouldExceedBudget(
  tokensSoFar: number,
  plannedCallTokens: number
): boolean {
  return tokensSoFar + plannedCallTokens > CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT;
}
