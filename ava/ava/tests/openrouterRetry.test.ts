// tests/openrouterRetry.test.ts — the retry loop in lib/openrouter.ts,
// run for real with global fetch() stubbed and fake timers (the real
// delays are 2s then 4s).
//
// Why this file exists: in the axios version the documented "retry on
// 429/5xx" was dead code (validateStatus: () => true stopped axios from
// throwing, and the plain Error thrown for a bad status failed
// axios.isAxiosError(), so status was always undefined and never
// retried) — and no test noticed, because none exercised it. These do.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { httpError, okCompletion, sentBody } from "./helpers/fetchStub";
import { callOpenRouterJSON } from "../lib/openrouter";

const fetchMock = vi.fn();
const GOOD = '{"reply":"ok","done":false}';

const call = () =>
  callOpenRouterJSON<{ reply: string; done: boolean }>({
    apiKey: "sk-test",
    model: "m",
    systemPrompt: "SYS",
    messages: [{ role: "user", content: "hi" }],
    maxOutputTokens: 123,
  });

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("retry on HTTP status (was dead code before Stage 4)", () => {
  it("429 once, then 200: succeeds after ONE retry (fetch called twice)", async () => {
    fetchMock.mockResolvedValueOnce(httpError(429)).mockResolvedValueOnce(okCompletion(GOOD));
    const p = call();
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(p).resolves.toEqual({ reply: "ok", done: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("500 then 503 then 200: three attempts, waiting 2s then 4s between them", async () => {
    fetchMock
      .mockResolvedValueOnce(httpError(500))
      .mockResolvedValueOnce(httpError(503))
      .mockResolvedValueOnce(okCompletion(GOOD));
    const p = call();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(fetchMock).toHaveBeenCalledTimes(1); // still inside the 2s backoff
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3_999);
    expect(fetchMock).toHaveBeenCalledTimes(2); // still inside the 4s backoff
    await vi.advanceTimersByTimeAsync(1);
    await expect(p).resolves.toEqual({ reply: "ok", done: false });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("persistent 500: gives up after 3 attempts total and throws the HTTP error (status attached)", async () => {
    fetchMock.mockImplementation(async () => httpError(500, "boom"));
    const p = call();
    const assertion = expect(p).rejects.toMatchObject({
      message: expect.stringContaining("OpenRouter error 500: boom"),
      status: 500,
      isOpenRouterHttpError: true,
    });
    await vi.advanceTimersByTimeAsync(6_000);
    await assertion;
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([400, 401, 402, 404])("%i is NOT retried — fails on the first attempt", async (status) => {
    fetchMock.mockResolvedValue(httpError(status));
    await expect(call()).rejects.toMatchObject({ status });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("retry on network-level failure (worked before, must still work)", () => {
  it("fetch rejects once (TypeError), then 200: succeeds after one retry", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed")).mockResolvedValueOnce(okCompletion(GOOD));
    const p = call();
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(p).resolves.toEqual({ reply: "ok", done: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("our own 60s timeout firing (TimeoutError) is treated as a network failure and retried", async () => {
    fetchMock
      .mockRejectedValueOnce(new DOMException("The operation timed out.", "TimeoutError"))
      .mockResolvedValueOnce(okCompletion(GOOD));
    const p = call();
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(p).resolves.toEqual({ reply: "ok", done: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("the connection dying while READING the body is also retried", async () => {
    const brokenBody = { status: 200, text: () => Promise.reject(new TypeError("terminated")) };
    fetchMock.mockResolvedValueOnce(brokenBody).mockResolvedValueOnce(okCompletion(GOOD));
    const p = call();
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(p).resolves.toEqual({ reply: "ok", done: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("persistent network failure: 3 attempts, then the LAST network error is thrown", async () => {
    fetchMock.mockImplementation(async () => { throw new TypeError("fetch failed"); });
    const p = call();
    const assertion = expect(p).rejects.toThrow("fetch failed");
    await vi.advanceTimersByTimeAsync(6_000);
    await assertion;
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe("content problems are NOT retried (same as before)", () => {
  it("model returns non-JSON text: one attempt, 'Model did not return valid JSON'", async () => {
    fetchMock.mockResolvedValue(okCompletion("sorry, I can't do that"));
    await expect(call()).rejects.toThrow(/Model did not return valid JSON/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("200 with an empty/missing completion: one attempt, 'no content'", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ choices: [] }), { status: 200 }));
    await expect(call()).rejects.toThrow(/no content/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("200 carrying an API-level error object: surfaces its message, one attempt", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: { message: "provider exploded" } }), { status: 200 }));
    await expect(call()).rejects.toThrow("OpenRouter error: provider exploded");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("200 with a non-JSON body: clear error, one attempt", async () => {
    fetchMock.mockResolvedValue(new Response("<html>gateway</html>", { status: 200 }));
    await expect(call()).rejects.toThrow(/non-JSON response/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("request shape", () => {
  it("non-streaming JSON request: POST, bearer key, json_object, max_tokens, NO stream flag, 60s abort signal", async () => {
    fetchMock.mockResolvedValue(okCompletion(GOOD));
    await call();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://openrouter.ai/api/v1/chat/completions");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer sk-test");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    const body = sentBody(fetchMock.mock.calls[0]);
    expect(body).not.toHaveProperty("stream");
    expect(body.response_format).toEqual({ type: "json_object" });
    expect(body.max_tokens).toBe(123);
    expect(body.messages[0]).toEqual({ role: "system", content: "SYS" });
  });

  it("a retried request is byte-identical to the first (same body, same key)", async () => {
    fetchMock.mockResolvedValueOnce(httpError(429)).mockResolvedValueOnce(okCompletion(GOOD));
    const p = call();
    await vi.advanceTimersByTimeAsync(2_000);
    await p;
    expect((fetchMock.mock.calls[1][1] as RequestInit).body).toBe((fetchMock.mock.calls[0][1] as RequestInit).body);
  });

  it("the API key never appears in a thrown error message", async () => {
    fetchMock.mockResolvedValue(httpError(400, "bad request"));
    await expect(call()).rejects.not.toThrow(/sk-test/);
  });
});
