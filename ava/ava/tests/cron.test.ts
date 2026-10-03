// tests/cron.test.ts — Stage 6. Both trigger paths for the two cron jobs:
//   scheduled()  — Cloudflare Cron Triggers (src/cron.ts), no auth, no Request
//   GET /cron/*  — manual/ad hoc routes behind CRON_SECRET
// and the one shared implementation underneath (run*()). The branch logic of
// the sweep itself (clean/infected/still-running/error/unrecognized) already
// has its own, unmodified tests: tests/pdfScanSweep*.test.ts.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";

vi.mock("../lib/supabase", () => ({
  findVerifiedReadySignups: vi.fn(),
  insertStoreRow: vi.fn(),
  updatePendingSignup: vi.fn(),
}));
vi.mock("../lib/pdfScanSweep", () => ({ sweepPdfScans: vi.fn() }));

import worker, { app } from "../src/index";
import { CRON_ACTIVATE_PENDING, CRON_SWEEP_PDF_SCANS, handleScheduled } from "../src/cron";
import { runActivatePending } from "../api/cron/activate-pending";
import { runSweepPdfScans } from "../api/cron/sweep-pdf-scans";
import { isCronAuthorizedRequest } from "../lib/cronAuth";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";
import { findVerifiedReadySignups, insertStoreRow, updatePendingSignup } from "../lib/supabase";
import { sweepPdfScans } from "../lib/pdfScanSweep";

const mFind = vi.mocked(findVerifiedReadySignups);
const mInsert = vi.mocked(insertStoreRow);
const mUpdate = vi.mocked(updatePendingSignup);
const mSweep = vi.mocked(sweepPdfScans);

// A row that satisfies the REAL toStoreRow() (lib/storeRow.ts is not mocked).
const ready = (id: string, over: Record<string, unknown> = {}) =>
  ({
    id, status: "verified_ready", phone_number_id: `pn-${id}`, store_name: `Store ${id}`,
    system_prompt: "You are a bot.", access_token: "tok", package: "low-tier",
    is_api_free: false, contact_number: `+1555${id}`, ...over,
  }) as never;

let env: Env;
let logSpy: ReturnType<typeof vi.spyOn>;
let errSpy: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  env = { LOCK: makeLockNamespace() };
  process.env.CRON_SECRET = "s3cret";
  mFind.mockReset().mockResolvedValue([]);
  mInsert.mockReset().mockResolvedValue({} as never);
  mUpdate.mockReset().mockResolvedValue(undefined);
  mSweep.mockReset().mockResolvedValue([]);
  logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
  errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  delete process.env.CRON_SECRET;
  logSpy.mockRestore();
  errSpy.mockRestore();
});

describe("runActivatePending — the one shared implementation", () => {
  it("activates every verified_ready row: insert via the real toStoreRow, THEN mark 'activated'", async () => {
    mFind.mockResolvedValue([ready("1"), ready("2")]);
    const s = await runActivatePending();
    expect(s).toEqual({ checked: 2, activated: 2, failed: 0, failures: [] });
    expect(mInsert).toHaveBeenCalledTimes(2);
    expect(mInsert.mock.calls[0]![0]).toMatchObject({ phone_number_id: "pn-1", store_name: "Store 1", package: "low-tier" });
    expect(mUpdate.mock.calls).toEqual([["1", { status: "activated" }], ["2", { status: "activated" }]]);
  });

  it("nothing to do -> { checked: 0, ... } and no writes", async () => {
    expect(await runActivatePending()).toEqual({ checked: 0, activated: 0, failed: 0, failures: [] });
    expect(mInsert).not.toHaveBeenCalled();
  });

  it("a row missing a required field fails alone: reported, NOT inserted, NOT marked, others still activate", async () => {
    mFind.mockResolvedValue([ready("1", { system_prompt: "  " }), ready("2")]);
    const s = await runActivatePending();
    expect(s.checked).toBe(2);
    expect(s.activated).toBe(1);
    expect(s.failed).toBe(1);
    expect(s.failures[0]!.id).toBe("1");
    expect(s.failures[0]!.error).toMatch(/system_prompt/);
    expect(mInsert).toHaveBeenCalledTimes(1);
    expect(mUpdate).toHaveBeenCalledTimes(1);
    expect(mUpdate).toHaveBeenCalledWith("2", { status: "activated" });
  });

  it("insert fails for one row -> that row is NOT marked activated (so it retries next pass); the rest go on", async () => {
    mFind.mockResolvedValue([ready("1"), ready("2")]);
    mInsert.mockRejectedValueOnce(new Error("Supabase insert failed: dup key")).mockResolvedValueOnce({} as never);
    const s = await runActivatePending();
    expect(s).toMatchObject({ checked: 2, activated: 1, failed: 1 });
    expect(mUpdate.mock.calls.map((c) => c[0])).toEqual(["2"]);
  });

  it("the lookup itself failing is the ONLY thing that throws", async () => {
    mFind.mockRejectedValue(new Error("db down"));
    await expect(runActivatePending()).rejects.toThrow("db down");
  });
});

describe("runSweepPdfScans — summary shape", () => {
  it("aggregates outcomes into checked/clean/infected/stillRunning/errors", async () => {
    mSweep.mockResolvedValue([
      { id: "a", result: "clean" }, { id: "b", result: "infected" },
      { id: "c", result: "still-running" }, { id: "d", result: "error", error: "boom" },
      { id: "e", result: "clean" },
    ]);
    expect(await runSweepPdfScans()).toEqual({
      checked: 5, clean: 2, infected: 1, stillRunning: 1,
      errors: [{ id: "d", result: "error", error: "boom" }],
    });
  });

  it("the lookup failing throws", async () => {
    mSweep.mockRejectedValue(new Error("db down"));
    await expect(runSweepPdfScans()).rejects.toThrow("db down");
  });
});

describe("scheduled() — Cloudflare Cron Triggers (no Request, no secret)", () => {
  const fire = (cron: string) => {
    const pending: Promise<unknown>[] = [];
    worker.scheduled({ cron, scheduledTime: Date.now() }, env, { waitUntil: (p) => void pending.push(p) });
    return pending;
  };

  it("*/15 -> runs activation only, under waitUntil, and logs the summary", async () => {
    mFind.mockResolvedValue([ready("1")]);
    const pending = fire(CRON_ACTIVATE_PENDING);
    expect(pending).toHaveLength(1);
    await Promise.all(pending);
    expect(mInsert).toHaveBeenCalledTimes(1);
    expect(mSweep).not.toHaveBeenCalled();
    expect(logSpy).toHaveBeenCalledWith(
      "[cron] activate-pending",
      JSON.stringify({ checked: 1, activated: 1, failed: 0, failures: [] })
    );
  });

  it("*/20 -> runs the sweep only, and logs the summary", async () => {
    mSweep.mockResolvedValue([{ id: "a", result: "clean" }]);
    await Promise.all(fire(CRON_SWEEP_PDF_SCANS));
    expect(mSweep).toHaveBeenCalledTimes(1);
    expect(mFind).not.toHaveBeenCalled();
    expect(logSpy).toHaveBeenCalledWith(
      "[cron] sweep-pdf-scans",
      JSON.stringify({ checked: 1, clean: 1, infected: 0, stillRunning: 0, errors: [] })
    );
  });

  it("an unrecognized expression THROWS (a visible failed invocation), and schedules nothing", () => {
    const pending: Promise<unknown>[] = [];
    expect(() => handleScheduled({ cron: "0 3 * * *", scheduledTime: 0 }, { waitUntil: (p) => void pending.push(p) }))
      .toThrow(/unrecognized cron expression "0 3 \* \* \*"/);
    expect(pending).toHaveLength(0);
  });

  it("a failed lookup rejects the waitUntil promise (so Cloudflare marks the run failed, not silently green)", async () => {
    mFind.mockRejectedValue(new Error("db down"));
    const [p] = fire(CRON_ACTIVATE_PENDING);
    await expect(p).rejects.toThrow("db down");
  });

  it("needs no CRON_SECRET: the secret belongs to the HTTP routes only", async () => {
    delete process.env.CRON_SECRET;
    mFind.mockResolvedValue([ready("1")]);
    await Promise.all(fire(CRON_ACTIVATE_PENDING));
    expect(mInsert).toHaveBeenCalledTimes(1);
  });

  it("wrangler.jsonc's triggers.crons are EXACTLY the two expressions src/cron.ts matches on (drift guard)", () => {
    const raw = fs.readFileSync(path.join(__dirname, "..", "wrangler.jsonc"), "utf8");
    const cfg = JSON.parse(raw.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n"));
    expect([...cfg.triggers.crons].sort()).toEqual([CRON_ACTIVATE_PENDING, CRON_SWEEP_PDF_SCANS].sort());
  });
});

describe("manual GET /cron/* routes — still behind CRON_SECRET", () => {
  const call = (p: string, init: RequestInit = {}) => app.request(p, init, env);
  const routes = ["/cron/activate-pending", "/cron/sweep-pdf-scans"];

  it.each(routes)("%s: POST -> 405 (checked before auth, like before)", async (r) => {
    expect((await call(r, { method: "POST" })).status).toBe(405);
  });

  it.each(routes)("%s: no secret / wrong secret / wrong Bearer -> 401, and NOTHING ran", async (r) => {
    expect((await call(r)).status).toBe(401);
    expect((await call(`${r}?secret=nope`)).status).toBe(401);
    expect((await call(r, { headers: { Authorization: "Bearer nope" } })).status).toBe(401);
    expect(mFind).not.toHaveBeenCalled();
    expect(mSweep).not.toHaveBeenCalled();
  });

  it.each(routes)("%s: CRON_SECRET unset -> 401 even when a secret is sent (fails closed)", async (r) => {
    delete process.env.CRON_SECRET;
    expect((await call(`${r}?secret=s3cret`)).status).toBe(401);
    expect((await call(`${r}?secret=`)).status).toBe(401);
    expect((await call(r, { headers: { Authorization: "Bearer " } })).status).toBe(401);
  });

  it.each(routes)("%s: repeated ?secret=..&secret=.. is rejected, like the Express array case", async (r) => {
    expect((await call(`${r}?secret=s3cret&secret=s3cret`)).status).toBe(401);
  });

  it("activate-pending: ?secret= and Authorization: Bearer both work and return the run summary", async () => {
    mFind.mockResolvedValue([ready("1")]);
    const a = await call("/cron/activate-pending?secret=s3cret");
    expect(a.status).toBe(200);
    expect(await a.json()).toEqual({ checked: 1, activated: 1, failed: 0, failures: [] });
    mFind.mockResolvedValue([]);
    const b = await call("/cron/activate-pending", { headers: { Authorization: "Bearer s3cret" } });
    expect(await b.json()).toEqual({ checked: 0, activated: 0, failed: 0, failures: [] });
  });

  it("sweep-pdf-scans: authorized -> 200 with the summary", async () => {
    mSweep.mockResolvedValue([{ id: "a", result: "infected" }]);
    const res = await call("/cron/sweep-pdf-scans?secret=s3cret");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ checked: 1, clean: 0, infected: 1, stillRunning: 0, errors: [] });
  });

  it("activate-pending lookup failure -> 500 with the old generic message, no leak", async () => {
    mFind.mockRejectedValue(new Error("password=hunter2 host=db.internal"));
    const res = await call("/cron/activate-pending?secret=s3cret");
    expect(res.status).toBe(500);
    const text = await res.text();
    expect(text).not.toContain("hunter2");
    expect(JSON.parse(text)).toEqual({ error: "Failed to look up verified_ready rows" });
  });

  it("sweep failure -> 500 { error: 'Sweep failed' }", async () => {
    mSweep.mockRejectedValue(new Error("boom"));
    const res = await call("/cron/sweep-pdf-scans?secret=s3cret");
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "Sweep failed" });
  });
});

describe("isCronAuthorizedRequest — same rules as the Express isCronAuthorized()", () => {
  const req = (url: string, auth?: string) =>
    new Request(url, { headers: auth ? { authorization: auth } : {} });
  it("matches the existing cronAuth.test.ts cases", () => {
    expect(isCronAuthorizedRequest(req("http://x/?secret=s3cret"))).toBe(true);
    expect(isCronAuthorizedRequest(req("http://x/", "Bearer s3cret"))).toBe(true);
    expect(isCronAuthorizedRequest(req("http://x/?secret=s3cre"))).toBe(false);      // different length
    expect(isCronAuthorizedRequest(req("http://x/", "Bearer nope"))).toBe(false);
    expect(isCronAuthorizedRequest(req("http://x/"))).toBe(false);                     // nothing sent
    expect(isCronAuthorizedRequest(req("http://x/?secret=s3cret&secret=s3cret"))).toBe(false); // array
  });
});
