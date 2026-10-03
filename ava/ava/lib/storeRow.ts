// lib/storeRow.ts
//
// This is the ONLY place that should ever decide package / is_api_free /
// api_key / ai_model. Do not set these fields anywhere else in the
// codebase — route every insert through buildStoreRow so the polarity
// bug from the spec's "known bug history" note cannot recur silently.
//
// PACKAGE NOTE: package is no longer always "basic" — it's gated on a
// referral_code (lib/promoCodes.ts): a valid code -> 'basic', anything
// else (invalid, expired, or simply not given) -> 'low-tier'. A valid
// code is now always ONE of two tiers (see lib/promoCodes.ts) — tier 1
// (is_trial=true, full free trial, no discount_expires_at) or tier 2
// (is_trial=false, paying from day one, discount_expires_at set) — both
// still land on package='basic'. The actual async DB validation (and
// tier decision) happens in api/chat.ts, NOT here — buildStoreRow()
// stays a pure synchronous function and just takes the already-decided
// ReferralApplication as an input.
//
// META-SETUP NOTE: buildStoreRow() no longer computes phone_number_id,
// access_token, or legal_name at all — those moved out of Ava's
// conversation entirely into POST /complete-setup (see
// api/complete-setup.ts), which sets them directly with a plain
// updatePendingSignup() call (no branching logic needed for a straight
// pass-through). This function's return type reflects that: it's
// StoreRow minus those three fields, not a full StoreRow — a full
// StoreRow only exists once toStoreRow() assembles one at activation
// time, by which point complete-setup has already filled them in.
import type {
  BusinessContext,
  ConversationMessage,
  ExtractionResult,
  PendingSignupRow,
  ReferralApplication,
  StoreRow,
} from "./types";

/**
 * Everything buildStoreRow() actually computes from Ava's conversation
 * — StoreRow minus the three fields that now come from
 * POST /complete-setup instead (phone_number_id, access_token,
 * legal_name).
 */
export type ChatDerivedStoreFields = Omit<
  StoreRow,
  "phone_number_id" | "access_token" | "legal_name"
>;

export function buildStoreRow(
  extracted: ExtractionResult,
  defaultAutoModel: string,
  // Not part of ExtractionResult on purpose — contact_number comes from
  // business_context (structured, given directly by the website), not
  // from the LLM extraction step re-parsing it out of free-text
  // conversation. See api/chat.ts.
  contactNumber: string | null = null,
  // null = no referral_code was given at all (same outcome as an
  // invalid one: package 'low-tier', nothing stored) — see
  // lib/promoCodes.ts for how a non-null value gets produced.
  referral: ReferralApplication | null = null
): ChatDerivedStoreFields {
  const byok = extracted.byok === true;
  const referralValid = referral?.valid === true;
  const isTier2 = referralValid && referral!.tier === 2;

  return {
    store_name: extracted.store_name,
    system_prompt: extracted.system_prompt,
    // 'basic' with ANY validated referral_code, tier 1 or tier 2 alike;
    // 'low-tier' is the default otherwise (invalid, expired, or no code
    // at all). Never conditional on BYOK — that's a separate axis
    // (is_api_free below).
    package: referralValid ? "basic" : "low-tier",
    // true = running on Boltane's shared platform key (default).
    // false = client brought their own OpenRouter key (BYOK).
    is_api_free: !byok,
    api_key: byok ? extracted.api_key : null,
    ai_model: byok ? extracted.ai_model || defaultAutoModel : null,
    contact_number: contactNumber,
    // Only stored when the referral_code was actually valid — an
    // invalid/missing code leaves this null rather than recording the
    // rejected attempt (nothing about a rejected attempt is recorded).
    referral_code: referralValid ? referral!.code : null,
    // Only meaningful when package === 'basic' (i.e. referralValid) —
    // true for tier 1, false for tier 2, null when there's no package
    // for it to describe at all ('low-tier').
    is_trial: referralValid ? !isTier2 : null,
    // Only tier 2 stacks a discount on top of 'basic' — tier 1 is
    // already fully free, and 'low-tier' never had a referral to begin
    // with.
    discount_expires_at: isTier2 ? referral!.discountExpiresAt ?? null : null,
  };
}

/**
 * Phase 2: Ava now writes to `pending_signups` instead of `stores`
 * directly. This builds on buildStoreRow() rather than duplicating its
 * branching — the package/is_api_free/api_key/ai_model values are
 * computed exactly once, here, at signup time.
 *
 * status is "chat_complete" at construction — Ava's conversation is
 * done, but the row still needs POST /complete-setup (legal_name,
 * phone_number_id, access_token) before OTP generation can happen —
 * see lib/otpHandoff.ts and api/complete-setup.ts.
 *
 * Only used for the no-contact_number fallback path now — a real
 * contact_number-tracked signup gets its core fields UPDATEd onto an
 * already-existing draft row instead (see api/chat.ts, which calls
 * buildStoreRow() directly for that case rather than this function).
 * NOTE: without a contact_number, POST /complete-setup has no way to
 * ever find this row again (it looks up strictly by contact_number) —
 * see the "Known limitation" flagged in the README and api/chat.ts's
 * header comment. This fallback path is kept for structural
 * completeness, not because it leads anywhere useful right now.
 */
export function buildPendingSignupRow(
  extracted: ExtractionResult,
  defaultAutoModel: string,
  contactNumber: string | null = null,
  referral: ReferralApplication | null = null
): PendingSignupRow {
  return {
    ...buildStoreRow(extracted, defaultAutoModel, contactNumber, referral),
    status: "chat_complete",
  };
}

/**
 * Builds a bare draft pending_signups row: just contact_number,
 * whatever business_context/conversation_history exist so far, and
 * status: 'draft'. None of StoreRow's fields are set yet — that's the
 * whole point of a draft (see PendingSignupRow's doc comment in
 * lib/types.ts). Used by api/chat.ts the first time a given
 * contact_number is seen with no existing row.
 */
export function buildDraftSignupRow(
  contactNumber: string,
  businessContext: BusinessContext | null,
  conversationHistory: ConversationMessage[]
): PendingSignupRow {
  return {
    contact_number: contactNumber,
    business_context: businessContext,
    conversation_history: conversationHistory,
    status: "draft",
  };
}

/**
 * The activation cron (GET /cron/activate-pending) calls this to
 * promote a verified_ready pending_signups row into the shape `stores`
 * expects.
 *
 * Deliberately does NOT re-derive package/is_api_free/api_key/ai_model
 * from raw onboarding data or re-run any BYOK/referral branching — it
 * only forwards the exact values already written onto the row, across
 * BOTH finalization steps (api/chat.ts's buildStoreRow() output for the
 * business-logic fields, api/complete-setup.ts's plain pass-through for
 * phone_number_id/access_token/legal_name). That's what keeps staging
 * and activation from ever being able to drift out of sync on this
 * logic: there is only one place each field's branching happens.
 *
 * PendingSignupRow's core fields are all optional (draft rows
 * genuinely don't have them yet — see lib/types.ts), but a row that's
 * actually reached 'verified_ready' must have been through BOTH
 * finalization steps already, so every field asserted here should be
 * present (legal_name excepted — see below). The runtime check exists
 * as a last-line-of-defense guard against a malformed row ever
 * reaching `stores`, not because this is expected to trip in normal
 * operation.
 */
export function toStoreRow(pending: PendingSignupRow): StoreRow {
  const required = [
    "phone_number_id",
    "store_name",
    "system_prompt",
    "access_token",
    "package",
    "is_api_free",
  ] as const;
  // An empty/blank string is as missing as null (a model that said
  // "done" too early can leave "" behind). Booleans (is_api_free=false)
  // are valid values and are only checked for null.
  const missing = required.filter((key) => {
    const v = pending[key];
    return v == null || (typeof v === "string" && v.trim() === "");
  });
  if (missing.length > 0) {
    throw new Error(
      `toStoreRow(): pending_signups row ${pending.id ?? "(no id)"} is missing required field(s) for activation: ${missing.join(", ")}. A row should never reach 'verified_ready' without these already set.`
    );
  }

  return {
    phone_number_id: pending.phone_number_id!,
    store_name: pending.store_name!,
    system_prompt: pending.system_prompt!,
    access_token: pending.access_token!,
    package: pending.package!,
    is_api_free: pending.is_api_free!,
    api_key: pending.api_key ?? null,
    ai_model: pending.ai_model ?? null,
    contact_number: pending.contact_number ?? null,
    referral_code: pending.referral_code ?? null,
    is_trial: pending.is_trial ?? null,
    discount_expires_at: pending.discount_expires_at ?? null,
    // NOT in the required list above — the column is genuinely
    // nullable at the DB level (see lib/types.ts's StoreRow doc
    // comment) and this function only enforces what activation
    // structurally can't proceed without.
    legal_name: pending.legal_name ?? null,
  };
}
