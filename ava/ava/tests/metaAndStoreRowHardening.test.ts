// tests/metaAndStoreRowHardening.test.ts
import { describe, it, expect, vi, afterEach } from "vitest";
import { resolveDisplayPhoneNumber } from "../lib/meta";
import { toStoreRow } from "../lib/storeRow";

const originalFetch = global.fetch;
afterEach(() => { global.fetch = originalFetch; });

describe("lib/meta — client-supplied phone_number_id cannot alter the URL", () => {
  it("encodes path/query characters and sets a timeout signal", async () => {
    const f = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ display_phone_number: "+1" }) });
    global.fetch = f as unknown as typeof fetch;
    await resolveDisplayPhoneNumber("../me?x=1#", "tok");
    const [url, opts] = f.mock.calls[0];
    expect(url).toBe("https://graph.facebook.com/v20.0/..%2Fme%3Fx%3D1%23?fields=display_phone_number");
    expect(opts.signal).toBeInstanceOf(AbortSignal);
  });
  it("a normal numeric id is unchanged", async () => {
    const f = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    global.fetch = f as unknown as typeof fetch;
    await resolveDisplayPhoneNumber("109364823947271", "tok");
    expect(f.mock.calls[0][0]).toBe("https://graph.facebook.com/v20.0/109364823947271?fields=display_phone_number");
  });
});

describe("toStoreRow — blank strings count as missing", () => {
  const good = { phone_number_id: "1", store_name: "S", system_prompt: "p", access_token: "t", package: "low-tier", is_api_free: true, status: "verified_ready" } as never;
  it("valid row still passes; is_api_free=false is a valid value, not 'missing'", () => {
    expect(() => toStoreRow(good)).not.toThrow();
    expect(() => toStoreRow({ ...(good as object), is_api_free: false } as never)).not.toThrow();
  });
  it("empty store_name / system_prompt are rejected", () => {
    expect(() => toStoreRow({ ...(good as object), store_name: "" } as never)).toThrow(/store_name/);
    expect(() => toStoreRow({ ...(good as object), system_prompt: "   " } as never)).toThrow(/system_prompt/);
  });
});
