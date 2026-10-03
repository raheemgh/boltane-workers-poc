// tests/cronAuth.test.ts
// Stage 7: ported from the Express `Request` shape to a Web `Request` when
// isCronAuthorized() (Express) was deleted. Same four tests, same intent
// ("rejects ... array" is now a repeated ?secret=a&secret=b, which is how an
// array reaches the check on a real request).
import { describe, it, expect, afterEach } from "vitest";
import { isCronAuthorizedRequest } from "../lib/cronAuth";

const req = (o: { auth?: string; secret?: string | string[] }) => {
  const url = new URL("http://x/cron/job");
  for (const s of o.secret === undefined ? [] : [o.secret].flat()) url.searchParams.append("secret", s);
  return new Request(url, { headers: o.auth ? { authorization: o.auth } : {} });
};

afterEach(() => { delete process.env.CRON_SECRET; });

describe("isCronAuthorizedRequest", () => {
  it("fails closed when CRON_SECRET is unset", () => {
    expect(isCronAuthorizedRequest(req({ auth: "Bearer x", secret: "x" }))).toBe(false);
  });
  it("accepts the Bearer header", () => {
    process.env.CRON_SECRET = "s3cret";
    expect(isCronAuthorizedRequest(req({ auth: "Bearer s3cret" }))).toBe(true);
  });
  it("accepts ?secret=", () => {
    process.env.CRON_SECRET = "s3cret";
    expect(isCronAuthorizedRequest(req({ secret: "s3cret" }))).toBe(true);
  });
  it("rejects wrong / different-length / repeated / missing values without throwing", () => {
    process.env.CRON_SECRET = "s3cret";
    expect(isCronAuthorizedRequest(req({ auth: "Bearer nope" }))).toBe(false);
    expect(isCronAuthorizedRequest(req({ secret: "s3cre" }))).toBe(false);
    expect(isCronAuthorizedRequest(req({ secret: ["s3cret", "s3cret"] }))).toBe(false);
    expect(isCronAuthorizedRequest(req({}))).toBe(false);
  });
});
