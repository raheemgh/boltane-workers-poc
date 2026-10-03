// lib/promoCodes.ts
//
// Validates a client-provided referral_code against `promo_codes`
// (promo_codes_table.sql) at signup finalization time (api/chat.ts).
//
// TWO-TIER DESIGN (confirmed): a code is invalid ONLY if it doesn't
// exist or is expired. free_trial_max_uses is NOT an overall cap
// anymore — a code never stops working once redeemed enough times, it
// only decides which tier a valid redemption lands on:
//   tier 1 (use_count < free_trial_max_uses): full free trial —
//     package='basic', is_trial=true, no discount_expires_at (nothing
//     stacked on top of free).
//   tier 2 (use_count >= free_trial_max_uses): paying from day one, but
//     still package='basic' — is_trial=false, discount_expires_at =
//     now() + 3 months.
// use_count increments on every VALID redemption, tier 1 or tier 2
// alike (it's a running "how many got the full trial" counter, not a
// total-uses-before-the-code-dies cap).
//
// See lib/storeRow.ts for how the ReferralApplication result of this
// feeds into buildStoreRow() (kept a pure function — this file does
// the async DB work, buildStoreRow() just takes the decided outcome).
import { getClient } from "./supabase";
import type { ReferralApplication } from "./types";

function threeMonthsFromNow(): string {
  const d = new Date();
  d.setMonth(d.getMonth() + 3);
  return d.toISOString();
}

/**
 * Checks a referral_code and, if valid (exists, not expired — nothing
 * else disqualifies it), determines its tier and atomically-ish
 * increments its use_count (see the race-condition note below). Never
 * throws for an invalid/expired code — that's a normal, expected
 * outcome (valid: false), not an error. Only throws on an actual
 * Supabase failure, which the caller (api/chat.ts) treats as "fail
 * safe to invalid" rather than blocking the whole signup over an
 * optional field — see that file's comment at the call site.
 *
 * RACE CONDITION NOTE: the expiry/tier check and the use_count
 * increment are two separate round-trips, not one atomic operation.
 * Two requests redeeming the same code at the exact same instant,
 * right at the tier boundary, could both read the same use_count and
 * both land on tier 1 even though only one of them "should" have,
 * strictly by increment order. Given this is an optional, low-stakes
 * onboarding discount (not payment or security-critical) and the
 * boundary is soft (being off by one redemption doesn't expose the
 * system the way exceeding a hard cap would), that's an accepted
 * trade-off rather than something this pass builds real atomicity for
 * — a Postgres function doing the read-tier-and-increment in one
 * statement would close this gap if it ever matters enough to be
 * worth building.
 */
export async function validateAndApplyReferralCode(
  code: string
): Promise<ReferralApplication> {
  const supabase = getClient();
  const nowIso = new Date().toISOString();

  const { data, error } = await supabase
    .from("promo_codes")
    .select("*")
    .eq("code", code)
    .maybeSingle();

  if (error) {
    throw new Error(`Supabase promo_codes lookup failed: ${error.message}`);
  }

  if (!data) {
    return { code, valid: false };
  }

  const row = data as {
    id: string;
    code: string;
    expires_at: string | null;
    free_trial_max_uses: number | null;
    use_count: number;
  };

  const expired = row.expires_at != null && row.expires_at <= nowIso;
  if (expired) {
    return { code, valid: false };
  }

  // null free_trial_max_uses = unlimited tier-1 redemptions (same
  // "null = unlimited" convention the old max_uses column had, now
  // applied to trial slots instead of total uses).
  const underTrialCap =
    row.free_trial_max_uses == null || row.use_count < row.free_trial_max_uses;
  const tier: 1 | 2 = underTrialCap ? 1 : 2;

  const { error: updateError } = await supabase
    .from("promo_codes")
    .update({ use_count: row.use_count + 1 })
    .eq("id", row.id);

  if (updateError) {
    throw new Error(`Supabase promo_codes update failed: ${updateError.message}`);
  }

  return {
    code,
    valid: true,
    tier,
    discountExpiresAt: tier === 2 ? threeMonthsFromNow() : null,
  };
}
