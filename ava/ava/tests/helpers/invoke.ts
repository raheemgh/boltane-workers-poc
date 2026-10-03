// tests/helpers/invoke.ts
//
// Transport adapter for testing Hono route handlers the way the old
// Express tests did: build a fake request (`{ body }`), run the real
// handler through a real Hono app, and read `_status` / `_json` back
// off a result object. Lets the existing assertions stay as they were
// (they are the regression evidence) while only the transport changes.
import { Hono } from "hono";
import type { Handler } from "hono";
import type { Env } from "../../src/env";

export interface FakeReq {
  method?: string;
  body?: unknown;
  /** Send this string as the raw body instead of JSON.stringify(body). */
  rawBody?: string;
}
export interface FakeRes {
  _status?: number;
  _json?: unknown;
}

export const makeReq = (body?: unknown, extra: Partial<FakeReq> = {}): FakeReq => ({
  method: "POST",
  body,
  ...extra,
});
export const makeRes = (): FakeRes => ({});

export async function invokeHandler(
  handler: Handler<{ Bindings: Env }>,
  path: string,
  env: Partial<Env>,
  req: FakeReq,
  res: FakeRes
): Promise<void> {
  const app = new Hono<{ Bindings: Env }>();
  app.all(path, handler);

  const method = req.method ?? "POST";
  const init: RequestInit = { method };
  if (method !== "GET" && method !== "HEAD") {
    init.headers = { "Content-Type": "application/json" };
    if (req.rawBody !== undefined) init.body = req.rawBody;
    else if (req.body !== undefined) init.body = JSON.stringify(req.body);
  }

  const r = await app.request(path, init, env as Env);
  res._status = r.status;
  const text = await r.text();
  try {
    res._json = JSON.parse(text);
  } catch {
    res._json = text;
  }
}
