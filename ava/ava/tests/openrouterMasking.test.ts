// tests/openrouterMasking.test.ts — runs the REAL lib/openrouter.ts
// (global fetch stubbed at the transport) to prove phone placeholders are
// unique across the whole request.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { okCompletion, sentBody } from "./helpers/fetchStub";

// Stage 4: the transport is now a stubbed global fetch() (was a mocked
// axios client + fake Readable stream). Every assertion below is
// unchanged — `posted[0]` is still "the JSON body that was sent".
const posted: Array<{ messages: Array<{ role: string; content: string }> }> = [];
let modelReply = "";
const fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => {
  posted.push(sentBody([_url, init]));
  return okCompletion(modelReply);
});

import { callOpenRouterJSON } from "../lib/openrouter";
import { createPhoneMasker } from "../lib/piiMasking";

beforeEach(() => {
  posted.length = 0;
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("createPhoneMasker", () => {
  it("numbers are unique across calls; the same number reuses its placeholder", () => {
    const m = createPhoneMasker();
    const a = m.mask("call 0999123456");
    const b = m.mask("manager 0988765432 and again 0999123456");
    expect(a).toBe("call [PHONE_1]");
    expect(b).toBe("manager [PHONE_2] and again [PHONE_1]");
    expect(m.restore("[PHONE_2] / [PHONE_1]")).toBe("0988765432 / 0999123456");
  });
});

describe("callOpenRouterJSON — real client, mocked transport", () => {
  it("two different numbers in two messages are restored to the RIGHT numbers", async () => {
    modelReply = JSON.stringify({ reply: "client [PHONE_1], manager [PHONE_2]", done: false });
    const out = await callOpenRouterJSON<{ reply: string }>({
      apiKey: "k", model: "m", systemPrompt: "SYS",
      messages: [
        { role: "user", content: "my whatsapp is 0999123456" },
        { role: "assistant", content: "thanks" },
        { role: "user", content: "manager's number is 0988765432" },
      ],
    });
    const seen = posted[0].messages.map((x) => x.content).join(" | ");
    expect(seen).not.toContain("0999123456");
    expect(seen).not.toContain("0988765432");
    expect(out.reply).toBe("client 0999123456, manager 0988765432");
  });

  it("the system prompt (which embeds business_context contact_number) is masked too, and restored on the way back", async () => {
    modelReply = JSON.stringify({ reply: "your number is [PHONE_1]", done: false });
    const out = await callOpenRouterJSON<{ reply: string }>({
      apiKey: "k", model: "m",
      systemPrompt: "- WhatsApp contact number: 0999123456",
      messages: [{ role: "user", content: "hi" }],
    });
    expect(posted[0].messages[0].content).toBe("- WhatsApp contact number: [PHONE_1]");
    expect(out.reply).toBe("your number is 0999123456");
  });

  it("text with no phone numbers is sent byte-for-byte unchanged", async () => {
    modelReply = JSON.stringify({ reply: "ok", done: false });
    await callOpenRouterJSON({ apiKey: "k", model: "m", systemPrompt: "SYS prompt", messages: [{ role: "user", content: "مرحبا" }] });
    expect(posted[0].messages.map((x) => x.content)).toEqual(["SYS prompt", "مرحبا"]);
  });
});
