// lib/meta.ts
//
// Read-only Graph API lookup, used only to put a human-readable phone
// number in the notification email to Raheem so he knows which number
// to expect the manual verification text FROM. This is NOT the
// self-send-via-Meta's-API path that was killed (that was about
// actively POSTing a message through the client's own credentials on
// first contact, which risked Meta's message-template requirement). A
// GET on /{phone_number_id}?fields=display_phone_number sends nothing
// and doesn't touch that requirement at all.
//
// Deliberately best-effort: if this lookup fails (bad/expired token,
// wrong ID, Meta hiccup), the caller falls back to showing
// phone_number_id instead — never blocks the OTP-code reply or the
// pending_signups insert on this succeeding.
const GRAPH_API_VERSION = "v20.0";
const FETCH_TIMEOUT_MS = 10_000;

export async function resolveDisplayPhoneNumber(
  phoneNumberId: string,
  accessToken: string
): Promise<string | null> {
  try {
    // phoneNumberId is client-supplied — encode it so it can never
    // alter the URL's path/query (e.g. "../me?x=").
    const url = `https://graph.facebook.com/${GRAPH_API_VERSION}/${encodeURIComponent(phoneNumberId)}?fields=display_phone_number`;
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${accessToken}` },
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!res.ok) return null;

    const data = (await res.json()) as { display_phone_number?: string };
    return data.display_phone_number ?? null;
  } catch {
    return null;
  }
}
