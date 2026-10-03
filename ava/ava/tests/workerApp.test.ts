// tests/workerApp.test.ts — the real Worker app (src/index.ts) end to
// end through Hono: CORS middleware ordering, method handling, and the
// onError safety net that replaces asyncHandler/errorMiddleware.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  findPendingSignupByContactNumber: vi.fn(),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../lib/otpHandoff", () => ({ triggerOtpHandoff: vi.fn() }));

import { app } from "../src/index";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";

const ORIGIN = "https://boltane.github.io";
let env: Env;

beforeEach(() => {
  env = { LOCK: makeLockNamespace() };
  process.env.RAHEEM_WHATSAPP_NUMBER = "+15559998888";
  process.env.ALLOWED_ORIGIN = ORIGIN;
});
afterEach(() => {
  delete process.env.ALLOWED_ORIGIN;
  vi.restoreAllMocks();
});

const post = (body: string, headers: Record<string, string> = {}) =>
  app.request(
    "/complete-setup",
    { method: "POST", headers: { "Content-Type": "application/json", Origin: ORIGIN, ...headers }, body },
    env
  );

describe("Worker app — /complete-setup transport behavior", () => {
  it("OPTIONS preflight: 204 with the allowed origin, no handler involved", async () => {
    const res = await app.request(
      "/complete-setup",
      { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "POST" } },
      env
    );
    expect(res.status).toBe(204);
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });

  it("no ALLOWED_ORIGIN configured: NO allow-origin header at all (fails closed)", async () => {
    delete process.env.ALLOWED_ORIGIN;
    const res = await post("{}");
    expect(res.headers.get("access-control-allow-origin")).toBeNull();
  });

  it("ALLOWED_ORIGIN is read per request, not at import time (replaces the old applyCors test)", async () => {
    process.env.ALLOWED_ORIGIN = "https://late.example";
    const res = await post("{}", { Origin: "https://late.example" });
    expect(res.headers.get("access-control-allow-origin")).toBe("https://late.example");
  });

  it("non-POST: 405 JSON, like the Express version", async () => {
    const res = await app.request("/complete-setup", { method: "GET" }, env);
    expect(res.status).toBe(405);
    expect(await res.json()).toEqual({ error: "Method not allowed" });
  });

  it("malformed JSON body: 400 { error: 'Invalid request' } (what errorMiddleware used to answer), not a 500", async () => {
    const res = await post("{not json");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "Invalid request" });
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });

  it("empty body: the handler's own 'Missing required field(s)' 400, same as Express gave", async () => {
    const res = await post("");
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toMatch(/Missing required field/);
  });

  it("an unexpected throw becomes a JSON 500 that leaks no detail and still carries the CORS header", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const broken = {
      LOCK: { idFromName: () => { throw new Error("secret internal detail"); }, get: () => { throw new Error("x"); } },
    } as unknown as Env;
    const res = await app.request(
      "/complete-setup",
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Origin: ORIGIN },
        body: JSON.stringify({ contact_number: "+1", legal_name: "L", phone_number_id: "1", access_token: "t" }),
      },
      broken
    );
    expect(res.status).toBe(500);
    const text = await res.text();
    expect(text).not.toContain("secret");
    expect(JSON.parse(text)).toEqual({ error: "Internal server error" });
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });
});
