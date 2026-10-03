// api/chat.ts
//
// POST /chat — stateless: the frontend sends the full conversation
// history + new message every turn; this function holds no session
// state of its own between requests. The one exception is
// contact_number tracking (see below), which uses Supabase itself as
// the durable state, not anything held in memory here.
//
// META SETUP MOVED OUT: phone_number_id, access_token, and legal_name
// are no longer collected here at all — Ava's conversational scope is
// now just business understanding + BYOK preference + optional
// referral_code. Those three fields instead come from a separate
// post-chat form on the site (POST /complete-setup, see
// api/complete-setup.ts), which is ALSO where OTP generation and the
// Raheem notification email now happen — this file no longer does
// either. Once the conversation model signals done: true, this file
// only does extraction + referral validation + finalizing the
// pending_signups row's business-logic fields (via buildStoreRow()) —
// it stops at status: 'chat_complete', not 'otp_sent'.
//
// KNOWN LIMITATION: POST /complete-setup looks a row up strictly by
// contact_number. A signup that completes this conversation WITHOUT
// ever having a contact_number tracked (business_context.contact_number
// was never sent on any call) lands in the same insert-only fallback
// path this file has always had (buildPendingSignupRow(), no
// draftRowId) — but that row is now an orphan: nothing can ever attach
// phone_number_id/access_token to it via /complete-setup, since there's
// no contact_number to look it up by. This wasn't asked to be fixed by
// making contact_number mandatory (a product decision, not made here)
// — flagging it plainly instead. See README's "Meta setup" section.
//
// --- contact_number tracking ---
// business_context (optional, see lib/types.ts) can include a
// contact_number — a plain WhatsApp number the website already
// collected. Whenever contact_number is present on a request:
//   - An existing pending_signups row for that number that already has
//     otp_code set means a signup for this number is already awaiting
//     activation — this request is BLOCKED with a support-contact
//     message, no LLM call, no state change.
//   - An existing row with no otp_code yet (still a draft, or mid-
//     conversation, or even chat_complete awaiting the post-chat form)
//     is a free overwrite: reused and updated in place, never
//     duplicated.
//   - No existing row: a fresh draft row is created (status: 'draft'),
//     so GET /lookup-signup has something to find if the client
//     abandons and returns later.
// Every turn that includes contact_number keeps that row's
// conversation_history current — see lib/types.ts's PendingSignupRow
// doc comment for why this needs to happen every turn, not just the
// first, given this API has no session memory of its own.
//
// --- token budgets ---
// See lib/tokenBudget.ts for the full reasoning. In short: every model
// call's OUTPUT is hard-capped via OpenRouter's own max_tokens
// (REPLY_MAX_OUTPUT_TOKENS / EXTRACTION_MAX_OUTPUT_TOKENS), and a
// cumulative per-attempt budget (CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT)
// is enforced via estimation from conversation_history before EVERY
// model call — reply and extraction alike. There is no input cap
// anywhere in this file.
//
// --- referral_code / package ---
// package now defaults to 'low-tier', not 'basic' — see
// lib/storeRow.ts's header comment. It only becomes 'basic' if the
// client gave a referral_code that validates against `promo_codes`
// (lib/promoCodes.ts) at finalization time, below, AND the global
// TRIAL_GLOBAL_CEILING safety net (lib/trialCeiling.ts) hasn't already
// been hit. referral_code's PRIMARY source is a plain top-level field
// on the request body (zero extra LLM cost, a direct lookup) — the
// AI-extracted ExtractedOnboardingData.referral_code is only a
// secondary fallback for when the frontend hasn't sent the top-level
// field. See ChatRequest's doc comment in lib/types.ts.
import type { Context } from "hono";
import { readJsonBody } from "../lib/httpBody";
import { callOpenRouterJSON } from "../lib/openrouter";
import {
  buildConversationSystemPrompt,
  buildExtractionSystemPrompt,
} from "../lib/systemPrompt";
import { buildDraftSignupRow, buildPendingSignupRow, buildStoreRow } from "../lib/storeRow";
import {
  findPendingSignupByContactNumber,
  insertPendingSignupRow,
  updatePendingSignup,
} from "../lib/supabase";
import { validateAndApplyReferralCode } from "../lib/promoCodes";
import { isTrialCeilingReached } from "../lib/trialCeiling";
import { sanitizeBusinessContext, sanitizeConversationHistory } from "../lib/sanitize";
import {
  EXTRACTION_MAX_OUTPUT_TOKENS,
  REPLY_MAX_OUTPUT_TOKENS,
  estimateAssistantTokensSoFar,
  estimateTokens,
  wouldExceedBudget,
} from "../lib/tokenBudget";
import type {
  BusinessContext,
  ChatRequest,
  ChatResponse,
  ConversationMessage,
  ExtractionResult,
  ReferralApplication,
} from "../lib/types";
import type { Env } from "../src/env";

function buildBlockedReply(raheemWhatsAppNumber: string): string {
  return [
    `It looks like a signup for this WhatsApp number is already in progress and awaiting activation. If you need help, please contact us directly at ${raheemWhatsAppNumber}.`,
    `يبدو أن هناك تسجيلاً لهذا الرقم على واتساب قيد التنفيذ بالفعل وينتظر التفعيل. إذا كنت بحاجة إلى مساعدة، يرجى التواصل معنا مباشرة على ${raheemWhatsAppNumber}.`,
  ].join("\n\n");
}

function buildBudgetExceededReply(raheemWhatsAppNumber: string): string {
  return [
    `This conversation has run longer than we can continue automatically. Please reach out to us directly at ${raheemWhatsAppNumber} and we'll help you finish signing up.`,
    `استمرت هذه المحادثة لفترة أطول مما يمكننا متابعته تلقائيًا. يرجى التواصل معنا مباشرة على ${raheemWhatsAppNumber} وسنساعدك على إكمال التسجيل.`,
  ].join("\n\n");
}

/**
 * The message shown once Ava's own conversation is done. No OTP code
 * here anymore — that now only exists after POST /complete-setup
 * succeeds (see api/complete-setup.ts's buildFinalInstructionalMessage()).
 * This just points the client to the next step on the site itself.
 */
function buildChatCompleteReply(): string {
  return [
    `Great, that's everything I need! Please continue to the next step on the site to connect your WhatsApp number and finish setting up your bot.`,
    `رائع، هذا كل ما أحتاجه! يرجى المتابعة إلى الخطوة التالية على الموقع لربط رقم واتساب الخاص بك وإكمال إعداد البوت.`,
  ].join("\n\n");
}

export default async function handler(c: Context<{ Bindings: Env }>): Promise<Response> {
  // CORS + OPTIONS preflight are handled by the middleware in
  // src/index.ts (they used to be applyCors(res) + a 204 here).
  if (c.req.method !== "POST") {
    return c.json({ error: "Method not allowed" }, 405);
  }

  const parsed = await readJsonBody(c.req.raw);
  if (!parsed.ok) {
    // What express.json() + errorMiddleware answered: 413 over the body
    // cap (lib/httpBody.ts), 400 for malformed JSON.
    return parsed.tooLarge
      ? c.json({ error: "Request body too large" }, 413)
      : c.json({ error: "Invalid request" }, 400);
  }
  const body = (parsed.value ?? {}) as Partial<ChatRequest>;
  // Runtime shape check — a malformed entry (null, missing content,
  // unknown role) used to throw outside any try/catch and, on Express 4,
  // crash the whole process. Valid messages pass through unchanged.
  const conversation_history: ConversationMessage[] =
    sanitizeConversationHistory(body.conversation_history);
  const user_message =
    typeof body.user_message === "string" ? body.user_message : "";

  if (!user_message.trim()) {
    return c.json({ error: "user_message is required" }, 400);
  }

  const apiKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey) {
    return c.json({ error: "Server misconfigured: missing OPENROUTER_API_KEY" }, 500);
  }

  const raheemWhatsAppNumber = process.env.RAHEEM_WHATSAPP_NUMBER;
  if (!raheemWhatsAppNumber) {
    return c.json({ error: "Server misconfigured: missing RAHEEM_WHATSAPP_NUMBER" }, 500);
  }

  const businessContext: BusinessContext | undefined = sanitizeBusinessContext(
    body.business_context
  );
  const contactNumber = businessContext?.contact_number?.trim() || null;
  // PRIMARY referral_code path — a plain top-level field, zero extra
  // LLM cost. See lib/types.ts's ChatRequest doc comment and the
  // fallback to extracted.referral_code in Step 2 below.
  const topLevelReferralCode =
    typeof body.referral_code === "string" ? body.referral_code.trim() || null : null;

  const conversationModel =
    process.env.AVA_CONVERSATION_MODEL || "openai/gpt-4o-mini";

  const turnMessages: ConversationMessage[] = [
    ...conversation_history,
    { role: "user", content: user_message },
  ];

  // --- token budget: check BEFORE the reply call, based on prior
  // assistant turns only (no input cap — see lib/tokenBudget.ts) ---
  const tokensSoFarBeforeReply = estimateAssistantTokensSoFar(conversation_history);
  if (wouldExceedBudget(tokensSoFarBeforeReply, REPLY_MAX_OUTPUT_TOKENS)) {
    const response: ChatResponse = {
      reply: buildBudgetExceededReply(raheemWhatsAppNumber),
      done: true,
      budgetExceeded: true,
    };
    return c.json(response, 200);
  }

  // --- contact_number tracking: dedupe check + draft row upsert ---
  let draftRowId: string | null = null;
  let mergedBusinessContext: BusinessContext | null = businessContext ?? null;

  if (contactNumber) {
    let existing;
    try {
      existing = await findPendingSignupByContactNumber(contactNumber);
    } catch (err) {
      console.error("contact_number lookup failed:", err);
      return c.json({ error: "Failed to check signup status" }, 500);
    }

    if (existing && existing.otp_code) {
      // Blocked: a signup for this number already has its OTP sent
      // (awaiting activation or further along). No LLM call, no state
      // change — see the "Duplicate registration" section in README.
      const response: ChatResponse = {
        reply: buildBlockedReply(raheemWhatsAppNumber),
        done: true,
        blocked: true,
      };
      return c.json(response, 200);
    }

    // Free overwrite (existing draft) or fresh draft — either way,
    // merge business_context so fields from an earlier turn survive a
    // later turn that only resends contact_number.
    mergedBusinessContext = businessContext
      ? { ...(existing?.business_context ?? {}), ...businessContext }
      : existing?.business_context ?? null;

    try {
      if (existing) {
        draftRowId = existing.id!;
        await updatePendingSignup(draftRowId, {
          business_context: mergedBusinessContext,
          conversation_history: turnMessages,
        });
      } else {
        const draftRow = buildDraftSignupRow(
          contactNumber,
          mergedBusinessContext,
          turnMessages
        );
        const inserted = await insertPendingSignupRow(draftRow);
        draftRowId = (inserted as { id: string }).id;
      }
    } catch (err) {
      console.error("Draft signup row upsert failed:", err);
      return c.json({ error: "Failed to save signup progress" }, 500);
    }
  }

  // --- Step 1: conversation turn ---
  let turn: { reply: string; done: boolean };
  try {
    turn = await callOpenRouterJSON<{ reply: string; done: boolean }>({
      apiKey,
      model: conversationModel,
      systemPrompt: buildConversationSystemPrompt(mergedBusinessContext ?? undefined),
      messages: turnMessages,
      maxOutputTokens: REPLY_MAX_OUTPUT_TOKENS,
    });
    // The model's JSON is not schema-checked upstream. A reply-less
    // object used to throw later, outside any try/catch.
    if (!turn || typeof turn.reply !== "string" || turn.reply.trim() === "") {
      throw new Error("Model response had no usable 'reply' string");
    }
  } catch (err) {
    console.error("Ava conversation call failed:", err);
    return c.json({ error: "Upstream model call failed" }, 502);
  }

  // Keep the draft row's conversation_history current with this turn's
  // reply too (not just the user's message), so a resumed session
  // picks up exactly where the client left off. Non-fatal: a resume
  // write failing shouldn't block the client's actual reply.
  if (draftRowId) {
    try {
      await updatePendingSignup(draftRowId, {
        conversation_history: [
          ...turnMessages,
          { role: "assistant", content: turn.reply },
        ],
      });
    } catch (err) {
      console.error("Draft conversation_history update failed (non-fatal):", err);
    }
  }

  if (!turn.done) {
    const response: ChatResponse = { reply: turn.reply, done: false };
    return c.json(response, 200);
  }

  // --- token budget: check BEFORE the extraction call too, now
  // accounting for the reply that was just generated ---
  const tokensSoFarBeforeExtraction =
    tokensSoFarBeforeReply + estimateTokens(turn.reply);
  if (wouldExceedBudget(tokensSoFarBeforeExtraction, EXTRACTION_MAX_OUTPUT_TOKENS)) {
    const response: ChatResponse = {
      reply: buildBudgetExceededReply(raheemWhatsAppNumber),
      done: true,
      budgetExceeded: true,
    };
    return c.json(response, 200);
  }

  // --- Step 2: extraction + referral validation + pending_signups
  // finalize. Stops at status: 'chat_complete' — OTP generation and
  // the Raheem notification email now happen in POST /complete-setup
  // instead (lib/otpHandoff.ts), once phone_number_id/access_token are
  // actually known. ---
  try {
    const extracted = await callOpenRouterJSON<ExtractionResult>({
      apiKey,
      model: conversationModel,
      systemPrompt: buildExtractionSystemPrompt(),
      messages: turnMessages,
      maxOutputTokens: EXTRACTION_MAX_OUTPUT_TOKENS,
    });

    const defaultAutoModel =
      process.env.DEFAULT_AUTO_AI_MODEL || "openai/gpt-4o-mini";

    // Optional — a missing/invalid code just means package stays
    // 'low-tier' (buildStoreRow()'s default when referral is null or
    // invalid). A DB error validating it fails SAFE to invalid rather
    // than blocking the whole signup over an optional field.
    //
    // PRIMARY source is the top-level request field (zero LLM cost,
    // exact lookup); extracted.referral_code (the client typing a code
    // into the chat itself) is only a secondary fallback for when the
    // frontend hasn't sent the top-level field.
    const referralCodeToUse = topLevelReferralCode || extracted.referral_code || null;

    let referral: ReferralApplication | null = null;
    if (referralCodeToUse) {
      try {
        // Global safety net checked BEFORE the code's own validation
        // (lib/trialCeiling.ts) — deliberately in this order, not
        // after: if the system-wide cap is already hit, there's no
        // reason to also consume one of the code's own limited uses
        // (validateAndApplyReferralCode() increments use_count) for a
        // signup that wouldn't get 'basic' anyway.
        const ceilingReached = await isTrialCeilingReached();
        if (ceilingReached) {
          referral = { code: referralCodeToUse, valid: false };
        } else {
          referral = await validateAndApplyReferralCode(referralCodeToUse);
        }
      } catch (err) {
        console.error("Referral code validation failed (failing safe to invalid):", err);
        referral = { code: referralCodeToUse, valid: false };
      }
    }

    if (draftRowId) {
      // A draft row already exists (contact_number was tracked this
      // session) — UPDATE it with the core fields rather than
      // inserting a second row for the same signup.
      const storeFields = buildStoreRow(extracted, defaultAutoModel, contactNumber, referral);
      await updatePendingSignup(draftRowId, { ...storeFields, status: "chat_complete" });
    } else {
      // No contact_number was ever provided this session — fall back
      // to the original insert-only path. See this file's header
      // comment's "KNOWN LIMITATION" note: this row is orphaned
      // without a contact_number for /complete-setup to find it by.
      const baseRow = buildPendingSignupRow(extracted, defaultAutoModel, null, referral);
      await insertPendingSignupRow(baseRow);
    }

    const response: ChatResponse = {
      reply: buildChatCompleteReply(),
      done: true,
    };
    return c.json(response, 200);
  } catch (err) {
    console.error("Ava extraction/pending_signups finalize failed:", err);
    // The model already produced a "we're done" reply, but the write
    // failed — surface a real error instead of a false success so the
    // frontend doesn't tell the client they're onboarded when they're not.
    return c.json({ error: "Failed to save onboarding data" }, 500);
  }
}
