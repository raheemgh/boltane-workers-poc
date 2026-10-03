// lib/supabase.ts
import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import type { PendingSignupRow, StoreRow } from "./types";

// NOTE on typing strategy: an untyped createClient() call makes this
// installed supabase-js version infer .insert()/.update() arguments
// as `never`, which fails `tsc --noEmit` (the project's "build"
// script) even though nothing is wrong at runtime — vitest doesn't
// type-check, so the test suite passing alone doesn't catch this. A
// hand-rolled Database<Tables/Row/Insert/Update> generic was tried
// first and didn't satisfy this version's internal
// SchemaNameOrClientOptions/GenericSchema constraints. Rather than
// keep guessing at undocumented internal generic requirements, the
// actual safety property this project cares about (package/is_api_free
// can't drift or be computed in more than one place) already lives one
// layer up, at the exported function signatures below (row: StoreRow,
// patch: Partial<PendingSignupRow>, etc.) and in buildStoreRow()
// (lib/storeRow.ts) being the single place that decides package (now
// referral-code-gated, not a fixed literal — see that file) and every
// other one of these fields. That's fully intact. What's below is a
// narrow, explicit `as any` at the three .insert()/.update() call sites
// only — every other method on this client (.from(), .select(),
// .storage, etc.) stays normally typed, and every PUBLIC function in
// this file keeps its real parameter types, so a caller anywhere else
// in the codebase still gets full type-checking on what it passes in.
let cachedClient: SupabaseClient | null = null;

export function getClient(): SupabaseClient {
  if (cachedClient) return cachedClient;

  const url = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!url || !serviceKey) {
    throw new Error(
      "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables"
    );
  }

  // Service role key — this file only ever runs server-side (a
  // Cloudflare Worker), never bundled into frontend code.
  //
  // No realtime.transport override needed here (there used to be one,
  // for Node 20's missing global WebSocket — see git history): Workers
  // has a native global WebSocket, so supabase-js's unconditional
  // RealtimeClient construction on createClient() just works, even
  // though this project never actually calls .channel()/.subscribe().
  // Confirmed in the migration audit's probe (`websocket_global` check)
  // before removing the `ws` package dependency.
  cachedClient = createClient(url, serviceKey);
  return cachedClient;
}

export async function insertStoreRow(row: StoreRow) {
  const supabase = getClient();
  const { data, error } = await supabase
    .from("stores")
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- see NOTE on typing strategy above
    .insert(row as any)
    .select()
    .single();

  if (error) {
    throw new Error(`Supabase insert failed: ${error.message}`);
  }

  return data;
}

/**
 * Ava's write path as of Phase 2 — writes to the staging table instead
 * of `stores` directly. `insertStoreRow` above is kept for the later
 * activation step to reuse (see lib/storeRow.ts's toStoreRow()).
 */
export async function insertPendingSignupRow(row: PendingSignupRow) {
  const supabase = getClient();
  const { data, error } = await supabase
    .from("pending_signups")
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- see NOTE on typing strategy above
    .insert(row as any)
    .select()
    .single();

  if (error) {
    throw new Error(`Supabase pending_signups insert failed: ${error.message}`);
  }

  return data;
}

export async function updatePendingSignup(
  id: string,
  patch: Partial<PendingSignupRow>
): Promise<void> {
  const supabase = getClient();
  const { error } = await supabase
    .from("pending_signups")
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- see NOTE on typing strategy above
    .update(patch as any)
    .eq("id", id);

  if (error) {
    throw new Error(`Supabase pending_signups update failed: ${error.message}`);
  }
}

/**
 * Eligibility gate for PDF upload. With OTP now fully manual, a row
 * only reaches 'otp_sent' -> 'verified_ready' via Raheem himself, so
 * requiring status='otp_sent' would actually be backwards for a
 * pre-review upload — a client can still attach a PDF while waiting on
 * Raheem, same as before. phone_number_id alone isn't a secret, but
 * there's no separate "verified" gate to check anymore since Raheem
 * verifies by hand off of the WhatsApp text + email, not through this
 * API. This just finds the client's own most recent staging row.
 */
export async function findEligiblePendingSignupForPdf(
  phoneNumberId: string
): Promise<PendingSignupRow | null> {
  const supabase = getClient();
  const { data, error } = await supabase
    .from("pending_signups")
    .select("*")
    .eq("phone_number_id", phoneNumberId)
    .in("status", ["otp_sent", "verified_ready"])
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();

  if (error) {
    throw new Error(`Supabase pending_signups lookup failed: ${error.message}`);
  }

  return (data as PendingSignupRow | null) ?? null;
}

/**
 * The most recent pending_signups row for a given contact_number,
 * regardless of status — used by api/chat.ts to decide between three
 * outcomes: no row (create a fresh draft), a row with no otp_code yet
 * (free overwrite — reuse/update it), or a row with otp_code already
 * set (blocked — a duplicate-registration attempt after OTP has
 * already gone out). Also used by GET /lookup-signup, which applies
 * its own, stricter otp_code-must-be-null check on the result rather
 * than exposing this function's "any status" behavior directly — see
 * that handler for why (it deliberately returns found:false rather
 * than distinguishing "never existed" from "already verified", to
 * avoid letting the lookup endpoint be used to probe which numbers
 * have completed signups).
 *
 * SECURITY NOTE: contact_number is a phone number, not a secret — this
 * lookup (and the /lookup-signup endpoint built on it) is only as safe
 * as "knowing someone's WhatsApp number" is as an access control. A
 * resumed draft's conversation_history can contain whatever the client
 * already typed, which may include their Meta access_token or
 * OpenRouter API key if they got that far before abandoning the
 * conversation. This is a real, deliberate trade-off, not an
 * oversight — see the README's "contact_number tracking" section.
 */
export async function findPendingSignupByContactNumber(
  contactNumber: string
): Promise<PendingSignupRow | null> {
  const supabase = getClient();
  const { data, error } = await supabase
    .from("pending_signups")
    .select("*")
    .eq("contact_number", contactNumber)
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();

  if (error) {
    throw new Error(`Supabase pending_signups lookup failed: ${error.message}`);
  }

  return (data as PendingSignupRow | null) ?? null;
}

/**
 * All rows Raheem has manually marked 'verified_ready' — the
 * activation cron's input set (GET /cron/activate-pending).
 */
export async function findVerifiedReadySignups(): Promise<PendingSignupRow[]> {
  const supabase = getClient();
  const { data, error } = await supabase
    .from("pending_signups")
    .select("*")
    .eq("status", "verified_ready");

  if (error) {
    throw new Error(`Supabase pending_signups lookup failed: ${error.message}`);
  }

  return (data as PendingSignupRow[] | null) ?? [];
}

// Read per call, not at module load (see the note in lib/openrouter.ts).
const pdfBucket = (): string => process.env.SUPABASE_PDF_BUCKET || "signup-pdfs";

/**
 * Uploads to a private Supabase Storage bucket (see
 * pdf_storage_bucket.sql — created with public: false, no anon/
 * authenticated policies, so only this service-role client can read
 * or write it). `upsert: true` so a re-upload cleanly replaces the
 * previous file at the same path.
 */
export async function uploadPdfToStorage(
  path: string,
  buffer: Buffer
): Promise<void> {
  const supabase = getClient();
  const { error } = await supabase.storage.from(pdfBucket()).upload(path, buffer, {
    contentType: "application/pdf",
    upsert: true,
  });

  if (error) {
    throw new Error(`Supabase Storage upload failed: ${error.message}`);
  }
}

/**
 * Deletes a previously-uploaded PDF — used only on an 'infected'
 * malware-scan verdict (lib/pdfScanSweep.ts). We store the file
 * synchronously at upload time (before the verdict is known — see
 * api/upload-pdf.ts and the README's "malware scan" section for why),
 * so an infected result means removing it after the fact rather than
 * never having written it.
 */
export async function deletePdfFromStorage(path: string): Promise<void> {
  const supabase = getClient();
  const { error } = await supabase.storage.from(pdfBucket()).remove([path]);

  if (error) {
    throw new Error(`Supabase Storage delete failed: ${error.message}`);
  }
}

/**
 * All rows currently waiting on a sed.sh verdict — the malware-scan
 * cron sweep's input set (lib/pdfScanSweep.ts).
 */
export async function findScanPendingSignups(): Promise<PendingSignupRow[]> {
  const supabase = getClient();
  const { data, error } = await supabase
    .from("pending_signups")
    .select("*")
    .eq("status", "scan_pending");

  if (error) {
    throw new Error(`Supabase pending_signups lookup failed: ${error.message}`);
  }

  return (data as PendingSignupRow[] | null) ?? [];
}
