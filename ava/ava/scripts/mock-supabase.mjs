// scripts/mock-supabase.mjs
//
// A tiny in-memory stand-in for the parts of Supabase's PostgREST API
// that POST /complete-setup touches (pending_signups: select by
// contact_number, update by id). Used ONLY by scripts/lock-check.mjs so
// the double-submit test is hermetic and repeatable — no real project,
// no real data. It also records side effects, so the test can assert
// "exactly ONE OTP was generated", which is stronger than counting
// HTTP statuses.
//
// With autoCreate (default, used by the /complete-setup lock test) every
// contact_number seen for the first time gets a 'chat_complete' row
// automatically. With autoCreate:false (used by the /chat check) an
// unknown number has NO row, and rows come from POST inserts / __seed. Each request is delayed (default 150ms) so that
// concurrent callers genuinely overlap inside the Worker.
//
// Stage 5/6 additions (all additive — the /complete-setup and /chat
// checks behave exactly as before): GET also filters by phone_number_id,
// status=eq.X and status=in.(a,b) (newest match wins, like
// order=created_at.desc&limit=1); POST/PUT /storage/v1/object/<bucket>/<path>
// records the uploaded bytes; DELETE /storage/v1/object/<bucket> removes
// objects; /__storage lists them; /__rows lists every row. Stage 6 adds
// POST /rest/v1/stores (what insertStoreRow() writes; recorded, listed at
// /__stores) and /__fail_store?phone_number_id=X (that store insert 500s once).
import http from "node:http";
import crypto from "node:crypto";

export function startMockSupabase({ port = 8799, delayMs = 150, autoCreate = true } = {}) {
  const rows = new Map(); // contact_number -> row
  const byId = new Map(); // id -> row
  const otpCount = new Map(); // contact_number -> number of PATCHes carrying otp_code
  const failNext = new Set(); // contact_numbers whose next OTP PATCH returns 500 once
  const storage = new Map(); // "bucket/path" -> { size, sha256, head }
  const stores = []; // rows POSTed to /rest/v1/stores
  const failStore = new Set(); // phone_number_ids whose next store insert returns 500 once
  let nextId = 1;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const json = (res, status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(body === undefined ? undefined : JSON.stringify(body));
  };
  const readBody = (req) =>
    new Promise((resolve) => {
      let data = "";
      req.on("data", (c) => (data += c));
      req.on("end", () => resolve(data));
    });
  const readBytes = (req) =>
    new Promise((resolve) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => resolve(Buffer.concat(chunks)));
    });

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://mock");

    // --- control endpoints for the test script ---
    if (url.pathname === "/__stats") {
      const key = url.searchParams.get("contact_number");
      const row = rows.get(key);
      return json(res, 200, { otp_count: otpCount.get(key) ?? 0, status: row?.status ?? null });
    }
    if (url.pathname === "/__row") {
      return json(res, 200, rows.get(url.searchParams.get("contact_number")) ?? null);
    }
    if (url.pathname === "/__seed") {
      const seeded = JSON.parse((await readBody(req)) || "{}");
      const row = { id: `row-${nextId++}`, ...seeded };
      rows.set(row.contact_number, row);
      byId.set(row.id, row);
      return json(res, 200, row);
    }
    if (url.pathname === "/__fail_next") {
      failNext.add(url.searchParams.get("contact_number"));
      return json(res, 200, { ok: true });
    }

    if (url.pathname === "/__rows") return json(res, 200, [...byId.values()]);
    if (url.pathname === "/__stores") return json(res, 200, stores);
    if (url.pathname === "/__fail_store") {
      failStore.add(url.searchParams.get("phone_number_id"));
      return json(res, 200, { ok: true });
    }
    if (url.pathname === "/__storage") {
      return json(res, 200, Object.fromEntries(storage));
    }

    // --- Storage API (what supabase-js's .storage.from(b).upload/remove call) ---
    if (url.pathname.startsWith("/storage/v1/object/")) {
      const rest = decodeURIComponent(url.pathname.slice("/storage/v1/object/".length));
      if (req.method === "POST" || req.method === "PUT") {
        const bytes = await readBytes(req);
        // supabase-js sends a Buffer/Blob body as multipart/form-data when it can;
        // unwrap the file part so the recorded hash is of the PDF itself.
        let payload = bytes;
        const ct = req.headers["content-type"] ?? "";
        const m = /boundary=(.+)$/.exec(ct);
        if (m) {
          const marker = Buffer.from(`--${m[1]}`);
          const start = bytes.indexOf(Buffer.from("\r\n\r\n"), bytes.indexOf(marker)) + 4;
          const end = bytes.lastIndexOf(Buffer.from(`\r\n--${m[1]}`));
          if (start > 3 && end > start) payload = bytes.subarray(start, end);
        }
        storage.set(rest, {
          size: payload.length,
          sha256: crypto.createHash("sha256").update(payload).digest("hex"),
          head: payload.subarray(0, 5).toString("latin1"),
        });
        return json(res, 200, { Key: rest });
      }
      if (req.method === "DELETE") {
        const bucket = rest.replace(/\/$/, "");
        const body = JSON.parse((await readBody(req)) || "{}");
        for (const p of body.prefixes ?? []) storage.delete(`${bucket}/${p}`);
        return json(res, 200, []);
      }
      return json(res, 405, { message: "mock: storage method not allowed" });
    }

    if (url.pathname === "/rest/v1/stores" && req.method === "POST") {
      const posted = JSON.parse((await readBody(req)) || "{}");
      const row = Array.isArray(posted) ? posted[0] : posted;
      if (failStore.delete(row.phone_number_id)) {
        return json(res, 500, { message: "mock: injected store insert failure" });
      }
      const stored = { id: `store-${stores.length + 1}`, ...row };
      stores.push(stored);
      const wantsObject = (req.headers.accept ?? "").includes("vnd.pgrst.object");
      return json(res, 201, wantsObject ? stored : [stored]);
    }

    if (url.pathname !== "/rest/v1/pending_signups") {
      return json(res, 404, { message: `mock: unhandled ${req.method} ${url.pathname}` });
    }

    await sleep(delayMs);

    if (req.method === "GET") {
      const wantsObj = (req.headers.accept ?? "").includes("vnd.pgrst.object");
      const eqParams = [...url.searchParams.entries()].filter(([k]) => !["select", "order", "limit"].includes(k));
      const onlyContact = eqParams.length === 1 && eqParams[0][0] === "contact_number";

      // Original behavior (kept byte-for-byte for /complete-setup + /chat): exact contact_number
      // lookup, auto-creating a chat_complete row on first sight unless autoCreate:false.
      if (onlyContact) {
        const eq = eqParams[0][1];
        if (!eq.startsWith("eq.")) return json(res, 400, { message: "mock: expected contact_number=eq.X" });
        const contact = eq.slice(3);
        let row = rows.get(contact);
        if (!row && !autoCreate) {
          return wantsObj
            ? json(res, 406, { code: "PGRST116", message: "JSON object requested, multiple (or no) rows returned" })
            : json(res, 200, []);
        }
        if (!row) {
          row = { id: `row-${nextId++}`, contact_number: contact, status: "chat_complete", store_name: "Mock Store" };
          rows.set(contact, row);
          byId.set(row.id, row);
        }
        return json(res, 200, wantsObj ? row : [row]);
      }

      // Stage 5/6: generic filters over every row (insertion order; newest match returned first).
      let matches = [...byId.values()];
      for (const [k, v] of eqParams) {
        if (v.startsWith("eq.")) matches = matches.filter((r) => String(r[k]) === v.slice(3));
        else if (v.startsWith("in.(") && v.endsWith(")")) {
          const set = v.slice(4, -1).split(",").map((x) => x.replace(/^"|"$/g, ""));
          matches = matches.filter((r) => set.includes(String(r[k])));
        } else return json(res, 400, { message: `mock: unsupported filter ${k}=${v}` });
      }
      matches.reverse(); // order=created_at.desc
      const lim = Number(url.searchParams.get("limit"));
      if (Number.isFinite(lim) && lim > 0) matches = matches.slice(0, lim);
      if (wantsObj) {
        return matches.length === 1
          ? json(res, 200, matches[0])
          : json(res, 406, { code: "PGRST116", message: "JSON object requested, multiple (or no) rows returned" });
      }
      return json(res, 200, matches);
    }

    if (req.method === "POST") {
      const posted = JSON.parse((await readBody(req)) || "{}");
      const row = { id: `row-${nextId++}`, ...(Array.isArray(posted) ? posted[0] : posted) };
      if (row.contact_number) rows.set(row.contact_number, row);
      byId.set(row.id, row);
      const wantsObject = (req.headers.accept ?? "").includes("vnd.pgrst.object");
      return json(res, 201, wantsObject ? row : [row]);
    }

    if (req.method === "PATCH") {
      const idEq = url.searchParams.get("id");
      const row = idEq?.startsWith("eq.") ? byId.get(idEq.slice(3)) : undefined;
      const patch = JSON.parse((await readBody(req)) || "{}");
      if (!row) return json(res, 404, { message: "mock: no such row" });

      if ("otp_code" in patch) {
        if (failNext.delete(row.contact_number)) {
          return json(res, 500, { message: "mock: injected failure" });
        }
        otpCount.set(row.contact_number, (otpCount.get(row.contact_number) ?? 0) + 1);
      }
      Object.assign(row, patch);
      res.writeHead(204);
      return res.end();
    }

    return json(res, 405, { message: "mock: method not allowed" });
  });

  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () =>
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => new Promise((r) => server.close(r)),
      })
    );
  });
}
