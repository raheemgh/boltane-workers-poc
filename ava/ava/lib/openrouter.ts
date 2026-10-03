// lib/openrouter.ts
//
// Thin wrapper around OpenRouter's chat completions endpoint. Both of
// Ava's own calls (conversation turn + extraction) ask the model to
// return a single JSON object and nothing else, so this file centralizes
// that contract instead of repeating it at each call site.
//
// Workers migration (Stage 4) — what this file used to do ONLY because
// Ava ran on a phone behind a mandatory SOCKS5 VPN (OpenRouter is
// geo-blocked in Syria), and what happened to each piece:
//
// - SOCKS5 proxy (OPENROUTER_SOCKS5_PROXY) + socks-proxy-agent + axios:
//   REMOVED. axios existed only because Node's fetch can't take a
//   socks-proxy-agent. A Worker calls OpenRouter from Cloudflare's own
//   network, so plain fetch() is enough.
// - stream: true + the hand-rolled SSE parser: REMOVED. Streaming was
//   never for incremental delivery (WhatsApp can't edit a message in
//   place) — it only kept bytes flowing so a SOCKS5 proxy wouldn't
//   treat a silent gap as a dead connection. One blocking request now.
//   (Still to be confirmed by timing a real completion from a real
//   deployment — see scripts/openrouter-smoke.mjs and CHANGES.md.)
// - OPENROUTER_API_BASE: KEPT, deliberately. It is now an unused escape
//   hatch by default (unset = call OpenRouter directly), but costs one
//   line and means a proxy/gateway can be put in front of OpenRouter
//   later without a code change. tests/openrouterApiBase.test.ts covers it.
//
// Kept as-is: 60s timeout and exponential backoff retry (2 retries: 2s,
// then 4s) on network failure or a 429/5xx. NOTE: the retry on 429/5xx
// was dead code in the axios version (validateStatus: () => true meant
// axios never threw on a bad status, and the plain Error thrown for it
// failed the axios.isAxiosError() check, so its status was always
// undefined and never retried). The fetch version below implements the
// behavior this comment always promised; tests/openrouterRetry.test.ts
// proves it.
import type { ConversationMessage } from "./types";
import { createPhoneMasker, restoreInValue } from "./piiMasking";

interface OpenRouterJSONParams {
  apiKey: string;
  model: string;
  systemPrompt: string;
  messages: ConversationMessage[];
  temperature?: number;
  // Output-token cap only — OpenRouter's `max_tokens` bounds generation,
  // not the prompt. There's deliberately no equivalent input cap
  // anywhere in this file (see the token-budget rules in
  // lib/tokenBudget.ts and README's "Token budgets" section).
  maxOutputTokens?: number;
}

const TIMEOUT_MS = 60_000;
const RETRY_DELAYS_MS = [2_000, 4_000]; // 2 attempts after the first, exponential
const DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableStatus(status: number | undefined): boolean {
  return status === 429 || (status !== undefined && status >= 500);
}

export async function callOpenRouterJSON<T>(
  params: OpenRouterJSONParams
): Promise<T> {
  const {
    apiKey,
    model,
    systemPrompt,
    messages,
    temperature = 0.4,
    maxOutputTokens,
  } = params;

  // Mask every conversation message before it leaves this process,
  // through ONE masker so placeholders are unique across the whole
  // request (see lib/piiMasking.ts). The system prompt goes through it
  // too: it embeds business_context (incl. the client's contact_number,
  // see lib/systemPrompt.ts's known-facts block), which is customer data.
  const masker = createPhoneMasker();
  const maskedSystemPrompt = masker.mask(systemPrompt);
  const maskedContents = messages.map((m) => masker.mask(m.content));

  const requestBody = {
    model,
    messages: [
      { role: "system", content: maskedSystemPrompt },
      ...messages.map((m, i) => ({
        role: m.role,
        content: maskedContents[i],
      })),
    ],
    response_format: { type: "json_object" },
    temperature,
    ...(maxOutputTokens ? { max_tokens: maxOutputTokens } : {}),
  };

  // Read per call (not cached at module load): on Workers the env is only
  // populated per request, and tests/openrouterApiBase.test.ts pins this down.
  const endpoint = process.env.OPENROUTER_API_BASE || DEFAULT_OPENROUTER_URL;

  let lastError: unknown;
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
    const isLastAttempt = attempt === RETRY_DELAYS_MS.length;

    // --- Network layer -------------------------------------------------
    // fetch() only THROWS for a genuine network-level failure (DNS,
    // connection reset, or our own AbortSignal.timeout firing). An HTTP
    // 429/5xx does NOT throw — it resolves normally — so the two cases
    // are handled in two separate places, unlike axios where one catch
    // covered both. Reading the body is inside this try too: a
    // connection dying mid-body is the same class of failure.
    let status: number;
    let responseText: string;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(requestBody),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      status = response.status;
      responseText = await response.text();
    } catch (err) {
      lastError = err;
      if (isLastAttempt) throw err;
      await sleep(RETRY_DELAYS_MS[attempt]);
      continue;
    }

    // --- HTTP layer ------------------------------------------------------
    if (status < 200 || status >= 300) {
      lastError = Object.assign(
        new Error(`OpenRouter error ${status}: ${responseText}`),
        { isOpenRouterHttpError: true, status }
      );
      if (isLastAttempt || !isRetryableStatus(status)) throw lastError;
      await sleep(RETRY_DELAYS_MS[attempt]);
      continue;
    }

    // --- Content layer: never retried (same as before — a model that
    // answered with garbage will usually answer with garbage again, and
    // the retry budget is for transport failures). --------------------------
    const parsed = parseJsonLoose<T>(extractContent(responseText));

    // Restore real phone numbers into every string field of the parsed
    // result, wherever it appears in the shape — covers both
    // { reply, done } and the extraction result without either call
    // site needing special-casing.
    return restoreInValue(parsed, masker.restore);
  }
  // Unreachable — the loop above always either returns or throws —
  // but keeps TypeScript's control-flow analysis happy.
  throw lastError;
}

/**
 * Pulls choices[0].message.content out of a non-streaming chat
 * completion body. Throws (non-retryable) if the body isn't JSON or
 * carries no text content.
 */
function extractContent(responseText: string): string {
  let data: {
    choices?: { message?: { content?: unknown } }[];
    error?: { message?: unknown };
  };
  try {
    data = JSON.parse(responseText);
  } catch {
    throw new Error(
      `OpenRouter returned a non-JSON response. First 200 chars: ${responseText.slice(0, 200)}`
    );
  }

  const content = data?.choices?.[0]?.message?.content;
  if (typeof content === "string" && content) return content;

  const apiMessage = data?.error?.message;
  throw new Error(
    typeof apiMessage === "string" && apiMessage
      ? `OpenRouter error: ${apiMessage}`
      : "OpenRouter response had no content"
  );
}

/**
 * Some models wrap JSON in ```json fences even when asked not to.
 * Strip that before parsing rather than failing the whole turn on it.
 */
function parseJsonLoose<T>(raw: string): T {
  const cleaned = raw
    .trim()
    .replace(/^```(json)?/i, "")
    .replace(/```$/, "")
    .trim();

  try {
    return JSON.parse(cleaned) as T;
  } catch {
    throw new Error(
      `Model did not return valid JSON. First 200 chars: ${cleaned.slice(0, 200)}`
    );
  }
}
