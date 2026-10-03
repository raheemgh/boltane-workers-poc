// tests/completeSetupInflight.test.ts — concurrent double-submit guard.
//
// Was: tested the old in-memory `inFlightSetups` Set directly. That Set
// is gone (see api/complete-setup.ts) — the guard is now the SetupLock
// Durable Object, so these tests go through the REAL SetupLock class
// via an in-process namespace (tests/helpers/lock.ts). The two original
// intents are kept as-is; the last two are new.
//
// What this does NOT prove: Durable Object platform behavior (global
// uniqueness, real workerd scheduling). That is scripts/concurrency-test.mjs
// against `wrangler dev`, and — for the cross-region part — Stage 7's
// real deploy.
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";
import { invokeHandler, makeReq, makeRes, type FakeReq, type FakeRes } from "./helpers/invoke";

vi.mock("../lib/supabase", () => ({
  findPendingSignupByContactNumber: vi.fn(),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../lib/otpHandoff", () => ({ triggerOtpHandoff: vi.fn() }));

import realHandler from "../api/complete-setup";
import { findPendingSignupByContactNumber } from "../lib/supabase";
import { triggerOtpHandoff } from "../lib/otpHandoff";

let env: Env;
const handler = (req: FakeReq, res: FakeRes) =>
  invokeHandler(realHandler, "/complete-setup", env, req, res);

const bodyFor = (contact_number: string) => ({
  contact_number,
  legal_name: "X LLC",
  phone_number_id: "123",
  access_token: "tok",
});
const body = bodyFor("+963900000000");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

beforeEach(() => {
  vi.clearAllMocks();
  env = { LOCK: makeLockNamespace() };
  process.env.RAHEEM_WHATSAPP_NUMBER = "+15559998888";
  vi.mocked(findPendingSignupByContactNumber).mockResolvedValue({ id: "r1", status: "chat_complete", store_name: "S" } as never);
});

describe("POST /complete-setup — double submit", () => {
  it("two simultaneous submits: exactly ONE OTP is generated, the other gets 409", async () => {
    let release!: () => void;
    vi.mocked(triggerOtpHandoff).mockImplementation(
      () => new Promise((r) => { release = () => r({ code: "111111111111" }); })
    );
    const r1 = makeRes(); const r2 = makeRes();
    const p1 = handler(makeReq(body), r1);
    await sleep(5); // let #1 reach the OTP step
    await handler(makeReq(body), r2);
    release(); await p1;

    expect(vi.mocked(triggerOtpHandoff)).toHaveBeenCalledTimes(1);
    expect(r1._status).toBe(200);
    expect(r2._status).toBe(409);
    expect((r2._json as { error: string }).error).toMatch(/already being processed/);
  });

  it("the guard is released afterwards (also after a failure) so a later retry works", async () => {
    vi.mocked(triggerOtpHandoff).mockRejectedValueOnce(new Error("db down"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const r1 = makeRes(); await handler(makeReq(body), r1);
    expect(r1._status).toBe(500);

    vi.mocked(triggerOtpHandoff).mockResolvedValueOnce({ code: "222222222222" });
    const r2 = makeRes(); await handler(makeReq(body), r2);
    expect(r2._status).toBe(200);
  });

  it("a burst of 5 simultaneous submits for one number: one 200, four 409, one OTP", async () => {
    let release!: () => void;
    vi.mocked(triggerOtpHandoff).mockImplementation(
      () => new Promise((r) => { release = () => r({ code: "333333333333" }); })
    );
    const results = Array.from({ length: 5 }, () => makeRes());
    const pending = results.map((r) => handler(makeReq(body), r));
    await sleep(10);
    release();
    await Promise.all(pending);

    const statuses = results.map((r) => r._status).sort();
    expect(statuses).toEqual([200, 409, 409, 409, 409]);
    expect(vi.mocked(triggerOtpHandoff)).toHaveBeenCalledTimes(1);
  });

  it("different contact_numbers never block each other", async () => {
    const releases: Array<() => void> = [];
    vi.mocked(triggerOtpHandoff).mockImplementation(
      () => new Promise((r) => { releases.push(() => r({ code: "444444444444" })); })
    );
    const a = makeRes(); const b = makeRes();
    const pa = handler(makeReq(bodyFor("+963900000001")), a);
    const pb = handler(makeReq(bodyFor("+963900000002")), b);
    await sleep(10);
    expect(vi.mocked(triggerOtpHandoff)).toHaveBeenCalledTimes(2); // both in flight at once
    releases.forEach((r) => r());
    await Promise.all([pa, pb]);
    expect([a._status, b._status]).toEqual([200, 200]);
  });
});
