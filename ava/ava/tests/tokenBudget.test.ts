// tests/tokenBudget.test.ts
import { describe, it, expect } from "vitest";
import {
  CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT,
  EXTRACTION_MAX_OUTPUT_TOKENS,
  REPLY_MAX_OUTPUT_TOKENS,
  estimateAssistantTokensSoFar,
  estimateTokens,
  wouldExceedBudget,
} from "../lib/tokenBudget";

describe("budget constants — within the specified ranges", () => {
  it("REPLY_MAX_OUTPUT_TOKENS is within 150-200", () => {
    expect(REPLY_MAX_OUTPUT_TOKENS).toBeGreaterThanOrEqual(150);
    expect(REPLY_MAX_OUTPUT_TOKENS).toBeLessThanOrEqual(200);
  });

  it("EXTRACTION_MAX_OUTPUT_TOKENS is within 1000-1200", () => {
    expect(EXTRACTION_MAX_OUTPUT_TOKENS).toBeGreaterThanOrEqual(1000);
    expect(EXTRACTION_MAX_OUTPUT_TOKENS).toBeLessThanOrEqual(1200);
  });

  it("CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT is 5000", () => {
    expect(CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT).toBe(5000);
  });
});

describe("estimateTokens — chars/4 heuristic", () => {
  it("rounds up", () => {
    expect(estimateTokens("abc")).toBe(1); // 3/4 -> ceil -> 1
    expect(estimateTokens("abcde")).toBe(2); // 5/4 -> ceil -> 2
  });

  it("empty string is 0 tokens", () => {
    expect(estimateTokens("")).toBe(0);
  });
});

describe("estimateAssistantTokensSoFar", () => {
  it("sums only assistant-role messages, ignoring user messages entirely", () => {
    const messages = [
      { role: "user" as const, content: "x".repeat(4000) }, // would be 1000 tokens if counted
      { role: "assistant" as const, content: "x".repeat(400) }, // 100 tokens
      { role: "user" as const, content: "hi" },
      { role: "assistant" as const, content: "x".repeat(400) }, // 100 tokens
    ];

    expect(estimateAssistantTokensSoFar(messages)).toBe(200);
  });

  it("empty conversation is 0", () => {
    expect(estimateAssistantTokensSoFar([])).toBe(0);
  });
});

describe("wouldExceedBudget", () => {
  it("false when comfortably under budget", () => {
    expect(wouldExceedBudget(1000, 200)).toBe(false);
  });

  it("false exactly at the boundary (not exceeding)", () => {
    expect(wouldExceedBudget(4800, 200)).toBe(false); // 4800+200=5000, not >5000
  });

  it("true just over the boundary", () => {
    expect(wouldExceedBudget(4801, 200)).toBe(true); // 5001 > 5000
  });

  it("true when already over budget before this call", () => {
    expect(wouldExceedBudget(6000, 200)).toBe(true);
  });
});
