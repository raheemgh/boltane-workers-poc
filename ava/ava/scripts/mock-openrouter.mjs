// scripts/mock-openrouter.mjs
//
// Stand-in for OpenRouter's chat-completions endpoint, used ONLY by
// scripts/chat-check.mjs so the /chat end-to-end check on workerd is
// hermetic (the sandbox it was written in can't reach openrouter.ai, and
// a repeatable test shouldn't spend money). The Worker is pointed at it
// through OPENROUTER_API_BASE — the override that Stage 4 deliberately kept.
//
// It records every request (auth header + body) so the test can assert on
// what actually crossed the wire: e.g. that a raw phone number NEVER
// leaves the Worker, and that no `stream` flag is sent.
//
// Behavior:
//   - max_tokens === 1200 (EXTRACTION_MAX_OUTPUT_TOKENS) -> extraction JSON
//   - else, conversation turn: if the last user message contains "DONE" ->
//     { reply, done: true }, otherwise { reply, done: false }. If any
//     message it received contains [PHONE_n] it echoes that placeholder
//     back, so the test can verify restore-on-the-way-back.
//   - GET /__requests  -> every recorded request;  POST /__fail?times=N&status=S
//     -> the next N requests fail with status S (429 by default).
import http from "node:http";

export function startMockOpenRouter({ port = 8798, delayMs = 50 } = {}) {
  const requests = [];
  let failTimes = 0;
  let failStatus = 429;

  const json = (res, status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  const readBody = (req) =>
    new Promise((resolve) => {
      let d = "";
      req.on("data", (c) => (d += c));
      req.on("end", () => resolve(d));
    });

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://mock");
    if (url.pathname === "/__requests") return json(res, 200, requests);
    if (url.pathname === "/__fail") {
      failTimes = Number(url.searchParams.get("times") ?? 1);
      failStatus = Number(url.searchParams.get("status") ?? 429);
      requests.length = 0;
      return json(res, 200, { ok: true });
    }

    const raw = await readBody(req);
    let body = {};
    try { body = JSON.parse(raw); } catch {}
    requests.push({ method: req.method, auth: req.headers.authorization ?? null, at: Date.now(), body });
    await new Promise((r) => setTimeout(r, delayMs));

    if (failTimes > 0) {
      failTimes--;
      return json(res, failStatus, { error: { message: `mock injected ${failStatus}` } });
    }

    const msgs = Array.isArray(body.messages) ? body.messages : [];
    const allText = msgs.map((m) => m.content).join("\n");
    const placeholder = /\[PHONE_\d+\]/.exec(allText)?.[0] ?? null;

    let content;
    if (body.max_tokens === 1200) {
      content = {
        store_name: "Mock Candles",
        ai_assistant_name: "Sami",
        business_description: "Sells handmade candles online.",
        byok: false,
        api_key: null,
        ai_model: null,
        referral_code: null,
        system_prompt: "You are Sami...",
      };
    } else {
      const lastUser = [...msgs].reverse().find((m) => m.role === "user")?.content ?? "";
      const done = /DONE/.test(lastUser);
      content = {
        reply: done ? "All set!" : `Nice to meet you${placeholder ? `, I will reach you on ${placeholder}` : ""}.`,
        done,
      };
    }
    return json(res, 200, { choices: [{ message: { role: "assistant", content: JSON.stringify(content) } }] });
  });

  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () =>
      resolve({ url: `http://127.0.0.1:${port}`, close: () => new Promise((r) => server.close(r)) })
    );
  });
}
