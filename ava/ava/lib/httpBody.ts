// lib/httpBody.ts
//
// JSON body reading for Hono handlers, matching what express.json({ limit:
// "4.5mb" }) did for the old Express handlers so no route's observable
// behavior changes:
//   - empty body            -> {}   (handler then answers its own
//                                    "missing field(s)" 400)
//   - malformed JSON        -> { ok: false }  (handler answers 400
//                                    { error: "Invalid request" }, the
//                                    same body errorMiddleware gave)
//   - body over the cap     -> { ok: false, tooLarge: true }  (handler
//                                    answers 413 { error: "Request body
//                                    too large" }, as errorMiddleware did)
// Hono's own c.req.json() would throw on all three, and a bare throw would
// turn into a 500 — a real behavior change for a client mistake.
//
// STAGE 7: the cap is new on Workers. server.ts's express.json() enforced
// 4.5mb (Vercel's old default) so a huge body was refused before it was
// parsed; req.text() has no such limit — a Worker request body can be up
// to 100 MB on the Free plan against a 128 MB isolate, so without this an
// oversized /chat or /complete-setup body would be buffered whole. Same
// two-layer approach as /upload-pdf (api/upload-pdf.ts): refuse on a
// declared Content-Length without reading a byte, and count bytes as they
// stream in for a chunked / absent / dishonest Content-Length.
export type JsonBody =
  | { ok: true; value: unknown }
  | { ok: false; tooLarge?: boolean };

/** What body-parser's "4.5mb" meant: 4.5 * 1024 * 1024 bytes. */
export const MAX_JSON_BODY_BYTES = Math.floor(4.5 * 1024 * 1024);

export async function readJsonBody(
  req: Request,
  maxBytes: number = MAX_JSON_BODY_BYTES
): Promise<JsonBody> {
  // 1. Honest clients declare their size: refuse without reading a byte.
  const declared = Number(req.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxBytes) {
    return { ok: false, tooLarge: true };
  }

  // No body at all -> same as an empty body.
  if (!req.body) return { ok: true, value: {} };

  // 2. Count as it streams; stop (and cancel the upstream read) the moment
  // the cap is crossed, so nothing past it is ever buffered.
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let seen = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      seen += value.byteLength;
      if (seen > maxBytes) {
        await reader.cancel().catch(() => {});
        return { ok: false, tooLarge: true };
      }
      chunks.push(value);
    }
  } catch {
    return { ok: false }; // the connection broke mid-body: a bad request, not a 500
  }

  const bytes = new Uint8Array(seen);
  let offset = 0;
  for (const c of chunks) {
    bytes.set(c, offset);
    offset += c.byteLength;
  }
  // Same decoding req.text() does (UTF-8, BOM stripped, malformed bytes -> U+FFFD).
  const text = new TextDecoder().decode(bytes);

  if (!text.trim()) return { ok: true, value: {} };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
}
