// tests/helpers/fetchStub.ts
//
// Shared pieces for tests that run the REAL lib/openrouter.ts with the
// global fetch() stubbed (replaces the old vi.mock("axios") + fake
// Readable stream). Only the transport is faked; everything in
// openrouter.ts — masking, request shaping, retry loop, parsing —
// is the real code.
export const okCompletion = (content: string): Response =>
  new Response(JSON.stringify({ choices: [{ message: { content } }] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

export const httpError = (status: number, body = `{"error":{"message":"upstream ${status}"}}`): Response =>
  new Response(body, { status, headers: { "Content-Type": "application/json" } });

/** The JSON body a fetch() call was made with. */
export const sentBody = (call: unknown[]): {
  model: string;
  messages: Array<{ role: string; content: string }>;
  [k: string]: unknown;
} => JSON.parse((call[1] as RequestInit).body as string);
