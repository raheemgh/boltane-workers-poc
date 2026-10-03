// tests/helpers/lock.ts
//
// In-process stand-in for the LOCK Durable Object namespace that hosts
// REAL SetupLock instances (src/setupLock.ts), one per name — same
// "same name -> same instance" contract the platform gives. It proves
// the class's own logic (check-and-set, release in `finally`) under
// Vitest; it does NOT prove Durable Object platform behavior — that is
// what scripts/concurrency-test.mjs against `wrangler dev` (workerd) is
// for, and what only a real deploy can fully confirm.
import { SetupLock } from "../../src/setupLock";
import type { Env } from "../../src/env";

export function makeLockNamespace(): Env["LOCK"] {
  const instances = new Map<string, SetupLock>();
  return {
    idFromName: (name: string) => name,
    get: (id) => {
      const key = id as unknown as string;
      let instance = instances.get(key);
      if (!instance) {
        instance = new SetupLock(undefined, undefined);
        instances.set(key, instance);
      }
      const target = instance;
      return { fetch: (request: Request) => target.fetch(request) };
    },
  };
}
