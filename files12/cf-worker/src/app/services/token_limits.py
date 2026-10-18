"""
Token-budget constants and a lightweight token estimator shared across
the webhook pipeline and the AI client — the "token firewall" that
protects the shared OpenRouter budget from a single chatty (or
bad-faith) conversation running up unbounded cost.

Four numbers, three of them enforced here/in webhook.py, one that's
guidance enforced upstream:

  MAX_INPUT_TOKENS_PER_MESSAGE   — a single incoming customer message
                                    over this is rejected before it
                                    ever reaches the model (webhook.py).
  MAX_OUTPUT_TOKENS_PER_MESSAGE  — sent as `max_tokens` on every
                                    OpenRouter call, no exceptions
                                    (ai_client.py).
  CONVERSATION_TOKEN_CAP         — cumulative input+output tokens
                                    allowed within one conversation
                                    window before the NEXT message
                                    forces a conversation reset (see
                                    is_new_conversation_and_touch in
                                    supabase_client.py).
  SYSTEM_PROMPT_TARGET_TOKENS_*  — guidance only. Store system_prompts
                                    are generated upstream in Ava and
                                    are expected to land in this range;
                                    nothing is enforced on that size
                                    here. What IS relevant here: this
                                    codebase does no separate "reserve
                                    N tokens for the system prompt,
                                    customer gets the rest" arithmetic
                                    against CONVERSATION_TOKEN_CAP — it
                                    tracks the conversation's ACTUAL
                                    measured usage (OpenRouter's real
                                    prompt_tokens, which already
                                    includes however large the system
                                    prompt really is, plus
                                    completion_tokens). That sidesteps
                                    the failure mode entirely: there's
                                    no hardcoded smaller number for the
                                    system prompt that the real prompt
                                    could quietly exceed, because
                                    nothing here assumes a size for it
                                    at all. These constants exist so
                                    that if any future code DOES need
                                    to reason about remaining budget,
                                    it reserves at least the top of
                                    this range (700), never less.

Real, provider-exact token counts are only available from OpenRouter
itself, in the `usage` object of a chat-completion response
(prompt_tokens + completion_tokens — see ai_client.py). Anywhere a
token count is needed BEFORE calling the API — checking an incoming
customer message's length — there is no way to get an exact count
without the target model's own tokenizer, so a conservative heuristic
estimator is used instead (see estimate_tokens below). It's
deliberately generous (rounds up): underestimating here is exactly
what would let an oversized message slip through uncapped, which is
the one failure mode this module exists to prevent.
"""

import math

# --- Per-message hard caps ---
MAX_INPUT_TOKENS_PER_MESSAGE = 300
MAX_OUTPUT_TOKENS_PER_MESSAGE = 300

# --- Per-conversation cumulative cap ---
# Once a conversation's running total (tracked in
# conversation_windows.token_count) reaches this, the next message
# from that customer forces a conversation reset: exactly like a
# natural window-expiry gap, it starts a fresh token count AND counts
# against the store's monthly conversation cap. See
# is_new_conversation_and_touch() in supabase_client.py and Step 3 of
# _process_message in webhook.py.
#
# Note on bounded overshoot: the reset triggers on the NEXT message
# after the cap is reached, not mid-message — so a single conversation
# can overshoot this cap by at most one more turn's worth of tokens
# (MAX_INPUT_TOKENS_PER_MESSAGE + MAX_OUTPUT_TOKENS_PER_MESSAGE = 600)
# before enforcement catches up. That's an accepted, tightly bounded
# tradeoff, not an oversight — catching it mid-stream would require
# knowing a response's token cost before the model finishes generating
# it, which isn't possible with a non-streaming call.
CONVERSATION_TOKEN_CAP = 3000

# --- Guidance only — see module docstring ---
SYSTEM_PROMPT_TARGET_TOKENS_MIN = 500
SYSTEM_PROMPT_TARGET_TOKENS_MAX = 700


def estimate_tokens(text: str) -> int:
    """
    Conservative pre-flight token estimate for text that hasn't been
    sent to a model yet (so no exact provider token count exists yet).
    Uses the common ~4-characters-per-token rule of thumb for English
    text, rounded UP — this function guards a hard limit, so it must
    never underestimate and let something oversized through.

    Text that HAS already been sent to OpenRouter should never use
    this — use the real `usage` field from that response instead (see
    ai_client.call_ai), since it's exact rather than estimated.
    """
    if not text:
        return 0
    return math.ceil(len(text) / 4)
