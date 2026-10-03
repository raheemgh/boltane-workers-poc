// tests/httpBody.test.ts — Stage 7. The 4.5mb JSON body cap that server.ts's
// express.json({ limit: "4.5mb" }) enforced and the Worker port had silently
// dropped (req.text() has no limit). Unit tests on readJsonBody + the same
// behavior seen from the real routes (/chat and /complete-setup).
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  findPendingSignupByContactNumber: vi.fn(),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../lib/otpHandoff", () => ({ triggerOtpHandoff: vi.fn() }));

import { app } from "../src/index";
import { MAX_JSON_BODY_BYTES, readJsonBody } from "../lib/httpBody";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";

const ORIGIN = "https://boltane.github.io";
const req = (body: RequestInit["body"], headers: Record<string, string> = {}) =>
  new Request("http://x/", { method: "POST", body, headers });

// a JSON document of exactly `n` bytes: {"a":"xxxx..."}
const jsonOfSize = (n: number) => `{"a":"${"x".repeat(n - 8)}"}`;

describe("readJsonBody", () => {
  it("the default cap equals body-parser's '4.5mb'", () => {
    expect(MAX_JSON_BODY_BYTES).toBe(4718592);
  });

  it("normal body -> parsed value", async () => {
    expect(await readJsonBody(req('{"a":1}'))).toEqual({ ok: true, value: { a: 1 } });
  });

  it("empty / whitespace / no body -> {}", async () => {
    expect(await readJsonBody(req(""))).toEqual({ ok: true, value: {} });
    expect(await readJsonBody(req("  \n "))).toEqual({ ok: true, value: {} });
    expect(await readJsonBody(new Request("http://x/", { method: "POST" }))).toEqual({ ok: true, value: {} });
  });

  it("malformed JSON -> { ok:false } with NO tooLarge flag (a 400, not a 413)", async () => {
    const r = await readJsonBody(req("{nope"));
    expect(r).toEqual({ ok: false });
    expect((r as { tooLarge?: boolean }).tooLarge).toBeUndefined();
  });

  it("exactly at the cap is accepted; one byte over is refused", async () => {
    expect((await readJsonBody(req(jsonOfSize(1000)), 1000)).ok).toBe(true);
    expect(await readJsonBody(req(jsonOfSize(1001)), 1000)).toEqual({ ok: false, tooLarge: true });
  });

  it("multi-byte UTF-8 is counted in BYTES, not characters, and decodes intact", async () => {
    const arabic = JSON.stringify({ m: "مرحبا بكم" }); // 2 bytes per letter
    const bytes = new TextEncoder().encode(arabic).length;
    expect(bytes).toBeGreaterThan(arabic.length);
    expect(await readJsonBody(req(arabic), bytes)).toEqual({ ok: true, value: { m: "مرحبا بكم" } });
    expect(await readJsonBody(req(arabic), bytes - 1)).toEqual({ ok: false, tooLarge: true });
  });

  it("a multi-byte character split across chunks still decodes correctly", async () => {
    const all = new TextEncoder().encode(JSON.stringify({ m: "مرحبا" }));
    const mid = all.length - 4; // lands inside a 2-byte letter
    const body = new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(all.slice(0, mid)); c.enqueue(all.slice(mid)); c.close(); },
    });
    const r = await readJsonBody(new Request("http://x/", { method: "POST", body, duplex: "half" } as RequestInit));
    expect(r).toEqual({ ok: true, value: { m: "مرحبا" } });
  });

  it("declared Content-Length over the cap: refused WITHOUT reading a single byte", async () => {
    let pulled = 0;
    const body = new ReadableStream<Uint8Array>(
      { pull(c) { pulled++; c.enqueue(new Uint8Array(100)); } },
      { highWaterMark: 0 }
    );
    const r = await readJsonBody(
      new Request("http://x/", { method: "POST", body, headers: { "content-length": "5000" }, duplex: "half" } as RequestInit),
      1000
    );
    expect(r).toEqual({ ok: false, tooLarge: true });
    expect(pulled).toBe(0);
  });

  it("no Content-Length (chunked): stops reading right after the cap, long before the end of a huge body", async () => {
    let pulled = 0;
    const body = new ReadableStream<Uint8Array>(
      { pull(c) { pulled++; c.enqueue(new Uint8Array(1000).fill(97)); if (pulled >= 5000) c.close(); } },
      { highWaterMark: 0 }
    );
    const r = await readJsonBody(
      new Request("http://x/", { method: "POST", body, duplex: "half" } as RequestInit),
      10_000
    );
    expect(r).toEqual({ ok: false, tooLarge: true });
    expect(pulled).toBeLessThan(20); // ~11 chunks cross the cap; 5000 would be the whole body
  });

  it("a body that dies mid-stream -> { ok:false } (400), never a thrown 500", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(new TextEncoder().encode('{"a":')); },
      pull(c) { c.error(new Error("socket reset")); },
    });
    const r = await readJsonBody(new Request("http://x/", { method: "POST", body, duplex: "half" } as RequestInit));
    expect(r).toEqual({ ok: false });
  });
});

describe("the real routes: 413 { error: 'Request body too large' } with the CORS header", () => {
  let env: Env;
  beforeEach(() => {
    env = { LOCK: makeLockNamespace() };
    process.env.RAHEEM_WHATSAPP_NUMBER = "+15559998888";
    process.env.ALLOWED_ORIGIN = ORIGIN;
  });
  afterEach(() => { delete process.env.ALLOWED_ORIGIN; delete process.env.RAHEEM_WHATSAPP_NUMBER; });

  const big = jsonOfSize(MAX_JSON_BODY_BYTES + 1);
  const hit = (path: string, body: string) =>
    app.request(path, { method: "POST", headers: { "Content-Type": "application/json", Origin: ORIGIN }, body }, env);

  it.each(["/chat", "/complete-setup"])("%s: body one byte over 4.5mb -> 413", async (path) => {
    const res = await hit(path, big);
    expect(res.status).toBe(413);
    expect(await res.json()).toEqual({ error: "Request body too large" });
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });

  it.each(["/chat", "/complete-setup"])("%s: malformed JSON is still a 400, not a 413", async (path) => {
    const res = await hit(path, "{nope");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "Invalid request" });
  });
});
