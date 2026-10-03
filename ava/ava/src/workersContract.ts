// src/workersContract.ts
//
// Compile-time only — nothing imports this at runtime and it adds nothing to
// the Worker bundle. It exists because src/index.ts is also type-checked by
// tsconfig.json (where Cloudflare's globals don't exist), so the real
// `ExportedHandler<Env>` contract can only be asserted here, under
// tsconfig.workers.json: the default export must be a valid Worker module
// (fetch + scheduled with Cloudflare's own ScheduledController/ExecutionContext).
import worker from "./index";
import type { Env } from "./env";

export const workerContract: ExportedHandler<Env> = worker;
