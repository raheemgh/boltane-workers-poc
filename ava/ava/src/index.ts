// src/index.ts
//
// The Worker entry point (wrangler.jsonc "main"). One Hono `app` serves every
// HTTP route; the default export also carries scheduled() for Cloudflare Cron
// Triggers, and the SetupLock Durable Object class is exported from here so
// the LOCK binding resolves. Routes: POST /chat, POST /complete-setup,
// GET /lookup-signup, POST /upload-pdf, the manual GET /cron/* fallbacks
// (CRON_SECRET), and GET /health.
//
// Replaced the Express entry point (server.ts, Render/Termux) in Stage 7.
// That one's asyncHandler/errorMiddleware/applyCors are the app.onError and
// cors() middleware below; its express.json({ limit: "4.5mb" }) is the
// body cap in lib/httpBody.ts; busboy is Request.formData() in
// api/upload-pdf.ts.
import { Hono } from "hono";
import { cors } from "hono/cors";
import completeSetupHandler from "../api/complete-setup";
import chatHandler from "../api/chat";
import uploadPdfHandler from "../api/upload-pdf";
import lookupSignupHandler from "../api/lookup-signup";
import activatePendingHandler from "../api/cron/activate-pending";
import sweepPdfScansHandler from "../api/cron/sweep-pdf-scans";
import { handleScheduled } from "./cron";
import type { CronController, Env, WaitUntilContext } from "./env";

// Durable Object classes must be exported from the Worker's entry
// module for the LOCK binding in wrangler.jsonc to resolve.
export { SetupLock } from "./setupLock";

export const app = new Hono<{ Bindings: Env }>();

// CORS (replaces lib/cors.ts's applyCors()): no Access-Control-Allow-Origin
// header at all when ALLOWED_ORIGIN is unset — fails closed, NOT an
// origin-echo fallback. Verified against Hono's actual cors() source, not
// assumed: it only sets the header when this function returns a truthy
// value, and (separately) only appends Vary: Origin / sets Allow-Methods /
// Allow-Headers because `origin` here is a function, not the literal string
// "*". One acknowledged, harmless difference from applyCors(): Hono sends
// Allow-Methods/Allow-Headers only on the OPTIONS/204 preflight, not on
// every response — browsers only consult those two on preflight. GET
// (/lookup-signup) and multipart POST (/upload-pdf) are CORS "simple"
// requests, so they need no preflight and no entry in allowMethods.
app.use(
  "*",
  cors({
    origin: () => process.env.ALLOWED_ORIGIN || undefined,
    allowMethods: ["POST", "OPTIONS"],
    allowHeaders: ["Content-Type"],
  })
);

// Replaces the old errorMiddleware/asyncHandler (Workers has no "unhandled
// rejection kills the process" failure mode, so only the part that matters
// is kept — a JSON 500 body, no detail leak).
// The CORS middleware above has already set its headers by the time a
// handler throws, so the browser sees the real 500, not a CORS error.
app.onError((err, c) => {
  console.error("Unhandled route error:", err);
  return c.json({ error: "Internal server error" }, 500);
});

// POST /complete-setup (Stage 3). app.all so the handler keeps its own
// 405 for non-POST methods, like the Express version did.
app.all("/complete-setup", completeSetupHandler);

// POST /chat (Stage 4). Same app.all + in-handler 405 convention.
app.all("/chat", chatHandler);

// POST /upload-pdf (Stage 5). Same app.all + in-handler 405 convention.
// multipart/form-data is a "simple" CORS content type, so the browser
// sends no preflight for it; the middleware above still answers one if
// a client does.
app.all("/upload-pdf", uploadPdfHandler);

// GET /lookup-signup (Stage 6). Mechanical port; 405 for non-GET stays in-handler.
app.all("/lookup-signup", lookupSignupHandler);

// Manual / ad hoc cron triggers (Stage 6), still behind CRON_SECRET. The
// primary mechanism is scheduled() below; these exist for "run it right
// now" and as an ops fallback. Both call the same run*() functions.
app.all("/cron/activate-pending", activatePendingHandler);
app.all("/cron/sweep-pdf-scans", sweepPdfScansHandler);

// Liveness check: confirms the Worker is up and routing, nothing more — no
// Supabase call, so it is cheap and can't contribute to any downstream limit.
app.get("/health", (c) => c.json({ ok: true }));

// A Worker exports BOTH entry points from one default object: fetch() for
// HTTP (the Hono app, incl. the manual /cron/* routes) and scheduled() for
// Cloudflare Cron Triggers. `app` is also a named export so tests can drive
// the HTTP side directly with app.request().
export default {
  fetch: app.fetch,
  scheduled(controller: CronController, _env: Env, ctx: WaitUntilContext): void {
    handleScheduled(controller, ctx);
  },
};
