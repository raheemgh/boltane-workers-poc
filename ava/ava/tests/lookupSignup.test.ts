// tests/lookupSignup.test.ts — Stage 6. GET /lookup-signup through the real
// Worker app. The thing this file exists for (the plan called it out):
// "no row" and "row exists but OTP already sent" must be INDISTINGUISHABLE
// to the caller — otherwise the endpoint becomes a probe for which phone
// numbers have completed signups. That privacy behavior had no test.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  findPendingSignupByContactNumber: vi.fn(),
}));

import { app } from "../src/index";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";
import { findPendingSignupByContactNumber } from "../lib/supabase";

const ORIGIN = "https://boltane.github.io";
const mFind = vi.mocked(findPendingSignupByContactNumber);
let env: Env;
let errSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  env = { LOCK: makeLockNamespace() };
  process.env.ALLOWED_ORIGIN = ORIGIN;
  mFind.mockReset();
  errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  delete process.env.ALLOWED_ORIGIN;
  errSpy.mockRestore();
});

const get = (qs: string, method = "GET") =>
  app.request(`/lookup-signup${qs}`, { method, headers: { Origin: ORIGIN } }, env);
const body = async (res: Response) => ({ status: res.status, json: await res.json() });

const HISTORY = [
  { role: "assistant", content: "Hi, what's your store called?" },
  { role: "user", content: "Mock Candles" },
];
const CONTEXT = { contact_number: "+15551230000", store_name: "Mock Candles" };

describe("GET /lookup-signup — privacy: nothing resumable always looks the same", () => {
  it("no row at all -> 200 { found: false } (not a 404)", async () => {
    mFind.mockResolvedValue(null);
    expect(await body(await get("?contact_number=%2B15551230000"))).toEqual({
      status: 200,
      json: { found: false },
    });
  });

  it("row exists but OTP already sent -> byte-identical to 'no row' (status, body, content-type)", async () => {
    mFind.mockResolvedValue(null);
    const none = await get("?contact_number=%2B15551230000");
    const noneText = await none.clone().text();

    mFind.mockResolvedValue({
      id: "r1",
      status: "otp_sent",
      otp_code: "123456",
      conversation_history: HISTORY,
      business_context: CONTEXT,
    } as never);
    const sent = await get("?contact_number=%2B15551230000");

    expect(sent.status).toBe(none.status);
    expect(await sent.text()).toBe(noneText);
    expect(sent.headers.get("content-type")).toBe(none.headers.get("content-type"));
    // and nothing from the row leaks into that response
    expect(noneText).not.toContain("Mock Candles");
  });
});

describe("GET /lookup-signup — resumable draft", () => {
  it("pre-OTP row -> found:true with the transcript + business_context to feed back into /chat", async () => {
    mFind.mockResolvedValue({
      id: "r1", status: "draft", otp_code: null,
      conversation_history: HISTORY, business_context: CONTEXT,
    } as never);
    const res = await get("?contact_number=%2B15551230000");
    expect(await body(res)).toEqual({
      status: 200,
      json: { found: true, conversation_history: HISTORY, business_context: CONTEXT },
    });
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });

  it("missing history/context columns come back as [] and null, never undefined", async () => {
    mFind.mockResolvedValue({ id: "r1", status: "draft", otp_code: null, conversation_history: null } as never);
    expect((await body(await get("?contact_number=1"))).json).toEqual({
      found: true, conversation_history: [], business_context: null,
    });
  });

  it("the contact_number is trimmed before the lookup", async () => {
    mFind.mockResolvedValue(null);
    await get("?contact_number=%20%2B15551230000%20");
    expect(mFind).toHaveBeenCalledWith("+15551230000");
  });
});

describe("GET /lookup-signup — request errors (same answers the Express version gave)", () => {
  it.each([["no param", ""], ["empty", "?contact_number="], ["whitespace only", "?contact_number=%20%20"]])(
    "%s -> 400, no DB call",
    async (_n, qs) => {
      const r = await body(await get(qs));
      expect(r).toEqual({ status: 400, json: { error: "contact_number is required" } });
      expect(mFind).not.toHaveBeenCalled();
    }
  );

  it("repeated ?contact_number=a&contact_number=b -> 400 (Express gave an array = not a string), not 'the first one'", async () => {
    const r = await body(await get("?contact_number=a&contact_number=b"));
    expect(r.status).toBe(400);
    expect(mFind).not.toHaveBeenCalled();
  });

  it("POST -> 405 JSON", async () => {
    expect(await body(await get("?contact_number=1", "POST"))).toEqual({
      status: 405, json: { error: "Method not allowed" },
    });
  });

  it("lookup throws -> 500 with a generic message, no internal detail", async () => {
    mFind.mockRejectedValue(new Error("connection to db.secret-host failed"));
    const res = await get("?contact_number=1");
    expect(res.status).toBe(500);
    const text = await res.text();
    expect(text).not.toContain("secret-host");
    expect(JSON.parse(text)).toEqual({ error: "Lookup failed" });
  });

  it("OPTIONS preflight is answered by the CORS middleware: 204 + allowed origin", async () => {
    const res = await app.request(
      "/lookup-signup",
      { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "GET" } },
      env
    );
    expect(res.status).toBe(204);
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });
});
