// tests/trialCeiling.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  getClient: vi.fn(),
}));

import {
  TRIAL_GLOBAL_CEILING,
  countActiveTrialsAcrossSystem,
  isTrialCeilingReached,
} from "../lib/trialCeiling";
import { getClient } from "../lib/supabase";

/**
 * A fake Supabase client modeling the real query builder's shape:
 * .from(table).select(...).eq("package","basic") returns an object
 * that is BOTH awaitable directly (the package-only fallback query)
 * AND further chainable via .eq("is_trial", true) (the primary,
 * is_trial-aware query) — matching how supabase-js's lazy
 * thenable builders actually behave.
 */
function makeFakeSupabase(config: {
  counts: Record<string, number>;
  missingIsTrialColumn?: boolean;
}) {
  const client = {
    from: vi.fn().mockImplementation((table: string) => ({
      select: vi.fn().mockImplementation(() => {
        const builder: Record<string, unknown> = {
          // lib/trialCeiling.ts now adds .neq("status","activated") on the
          // pending_signups query (see that file) — chainable no-op here,
          // since this fake works from fixed counts, not real rows.
          neq: vi.fn().mockImplementation(() => builder),
          eq: vi.fn().mockImplementation((_col1: string, _val1: unknown) => {
          const packageOnlyResult = Promise.resolve({
            count: config.counts[table] ?? 0,
            error: null,
          });
          return {
            eq: vi.fn().mockImplementation((_col2: string, _val2: unknown) => {
              if (config.missingIsTrialColumn) {
                return Promise.resolve({
                  count: null,
                  error: {
                    code: "42703",
                    message: `column ${table}.is_trial does not exist`,
                  },
                });
              }
              return Promise.resolve({
                count: config.counts[table] ?? 0,
                error: null,
              });
            }),
            then: packageOnlyResult.then.bind(packageOnlyResult),
            catch: packageOnlyResult.catch.bind(packageOnlyResult),
          };
        }),
        };
        return builder;
      }),
    })),
  };

  return { client };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, "log").mockImplementation(() => {});
});

describe("countActiveTrialsAcrossSystem", () => {
  it("sums pending_signups + stores counts (is_trial column present)", async () => {
    const { client } = makeFakeSupabase({
      counts: { pending_signups: 5, stores: 10 },
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const total = await countActiveTrialsAcrossSystem();
    expect(total).toBe(15);
  });

  it("falls back to package-only counting when is_trial doesn't exist yet, without throwing", async () => {
    const { client } = makeFakeSupabase({
      counts: { pending_signups: 3, stores: 4 },
      missingIsTrialColumn: true,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const total = await countActiveTrialsAcrossSystem();
    expect(total).toBe(7);
  });
});

describe("isTrialCeilingReached", () => {
  it("false when comfortably under the ceiling", async () => {
    const { client } = makeFakeSupabase({
      counts: { pending_signups: 10, stores: 20 }, // 30 total, ceiling is 80
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    expect(await isTrialCeilingReached()).toBe(false);
    expect(console.log).not.toHaveBeenCalled();
  });

  it("false one under the ceiling (79 < 80)", async () => {
    const { client } = makeFakeSupabase({
      counts: { pending_signups: 39, stores: 40 }, // 79 total
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    expect(await isTrialCeilingReached()).toBe(false);
  });

  it("true exactly at the ceiling (80 >= 80)", async () => {
    const { client } = makeFakeSupabase({
      counts: { pending_signups: 40, stores: 40 }, // exactly 80
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    expect(await isTrialCeilingReached()).toBe(true);
    expect(console.log).toHaveBeenCalledTimes(1);
    expect(vi.mocked(console.log).mock.calls[0][0]).toContain(
      String(TRIAL_GLOBAL_CEILING)
    );
  });

  it("true when over the ceiling", async () => {
    const { client } = makeFakeSupabase({
      counts: { pending_signups: 50, stores: 50 }, // 100 total
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    expect(await isTrialCeilingReached()).toBe(true);
  });
});
