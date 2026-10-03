// tests/openrouterApiBase.test.ts — OPENROUTER_API_BASE lets requests be
// routed through an alternate endpoint (e.g. a Cloudflare Worker proxy)
// without changing anything else about the request/response contract.
//
// Stage 4: transport changed from a mocked axios client to a stubbed
// global fetch(); the three intents and their assertions are unchanged
// (`posted[i].url` is now `fetchMock.mock.calls[i][0]`).
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { okCompletion } from "./helpers/fetchStub";
import { callOpenRouterJSON } from "../lib/openrouter";

const fetchMock = vi.fn();

const call = () =>
  callOpenRouterJSON({ apiKey: "k", model: "m", systemPrompt: "SYS", messages: [{ role: "user", content: "hi" }] });

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => okCompletion('{"reply":"ok","done":false}'));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.OPENROUTER_API_BASE;
});

describe("OPENROUTER_API_BASE override", () => {
  it("defaults to the real OpenRouter endpoint when unset", async () => {
    await call();
    expect(fetchMock.mock.calls[0][0]).toBe("https://openrouter.ai/api/v1/chat/completions");
  });

  it("posts to the override URL instead when set (e.g. a Worker proxy)", async () => {
    process.env.OPENROUTER_API_BASE = "https://ava-proxy.example.workers.dev";
    await call();
    expect(fetchMock.mock.calls[0][0]).toBe("https://ava-proxy.example.workers.dev");
  });

  it("is read per call, not cached — a later call sees a changed env value", async () => {
    await call();
    expect(fetchMock.mock.calls[0][0]).toBe("https://openrouter.ai/api/v1/chat/completions");
    process.env.OPENROUTER_API_BASE = "https://ava-proxy.example.workers.dev";
    await call();
    expect(fetchMock.mock.calls[1][0]).toBe("https://ava-proxy.example.workers.dev");
  });
});
