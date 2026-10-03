// lib/trialCeiling.ts
//
// A blunt, deliberately simple safety net on TOP OF each promo code's
// own free_trial_max_uses limit (lib/promoCodes.ts) — not a
// replacement for it. Protects against total exposure if more referral
// codes get added later than originally planned: even if every
// individual code still has trial slots left, the system-wide count of
// active free trials is capped at TRIAL_GLOBAL_CEILING.
//
// Counts package='basic' AND is_trial=true rows across BOTH
// `pending_signups` (this repo, migration in pending_signups_table.sql)
// and `stores` (the core engine's table — not owned by this repo, but
// the same Supabase database, so a direct count query against it is
// legitimate here). is_trial=true means tier 1 (full free trial, see
// lib/promoCodes.ts) — a tier-2 'basic' row (paying from day one, just
// with a stacked discount) deliberately does NOT count against this
// ceiling, since it isn't consuming free-trial capacity.
//
// FALLBACK: if a query against is_trial fails with an "undefined
// column" error (e.g. this table's migration hasn't been applied in
// some environment yet), this falls back to counting every
// package='basic' row directly. That fallback is deliberately
// over-inclusive now that tiers exist — it would count tier-2 rows as
// if they were trials too — but over-counting is the safe failure mode
// for a protective ceiling (it can only make the cap trigger more
// readily, never less), so this is an acceptable approximation for a
// path that's only meant to be hit during deploy-order skew, not
// normal operation.
import { getClient } from "./supabase";

export const TRIAL_GLOBAL_CEILING = 80;

function isMissingColumnError(error: { code?: string; message?: string } | null): boolean {
  if (!error) return false;
  // Postgres' own undefined_column error code, surfaced through
  // PostgREST as-is.
  if (error.code === "42703") return true;
  return /column .*is_trial.* does not exist/i.test(error.message ?? "");
}

// A pending_signups row that has been activated is COPIED into `stores`
// by the activation cron (the pending row stays, with status
// 'activated') — counting both would count one customer twice and make
// the ceiling trigger at half its intended value. So activated pending
// rows are excluded; the `stores` copy is what counts for them.
function selectCount(table: "pending_signups" | "stores") {
  const q = getClient()
    .from(table)
    .select("*", { count: "exact", head: true });
  return table === "pending_signups" ? q.neq("status", "activated") : q;
}

async function countBasicTrials(table: "pending_signups" | "stores"): Promise<number> {
  let { count, error } = await selectCount(table)
    .eq("package", "basic")
    .eq("is_trial", true);

  if (error && isMissingColumnError(error)) {
    // is_trial query failed (column missing in this environment) —
    // fall back to counting every package='basic' row directly. See
    // header comment: this over-counts tier-2 rows as trials, but
    // that's the safe direction to be wrong in for a protective cap.
    ({ count, error } = await selectCount(table).eq("package", "basic"));
  }

  if (error) {
    throw new Error(`Supabase count failed on ${table}: ${error.message}`);
  }

  return count ?? 0;
}

/**
 * Total active 'basic' trials across both tables combined.
 */
export async function countActiveTrialsAcrossSystem(): Promise<number> {
  const [pendingCount, storesCount] = await Promise.all([
    countBasicTrials("pending_signups"),
    countBasicTrials("stores"),
  ]);
  return pendingCount + storesCount;
}

/**
 * true if the system is already at or above TRIAL_GLOBAL_CEILING — the
 * caller (api/chat.ts) treats this as "act as if the referral code
 * were invalid for this signup" (package 'low-tier' instead of
 * 'basic'), regardless of the code's own remaining free_trial_max_uses.
 * Logs when it trips so Raheem notices this happening rather than it
 * silently capping signups.
 */
export async function isTrialCeilingReached(): Promise<boolean> {
  const total = await countActiveTrialsAcrossSystem();
  const reached = total >= TRIAL_GLOBAL_CEILING;

  if (reached) {
    console.log(
      `[trialCeiling] TRIAL_GLOBAL_CEILING (${TRIAL_GLOBAL_CEILING}) reached: ${total} active 'basic' trials across stores+pending_signups. Treating this referral code as invalid for this signup; granting 'low-tier' instead.`
    );
  }

  return reached;
}
