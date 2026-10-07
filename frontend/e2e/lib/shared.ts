/**
 * The one onboarded instance every stateless spec shares.
 *
 * The first worker that asks for it starts it and completes onboarding; the
 * others wait on a lock directory and then reuse it. Only its name and URL are
 * written to the run directory, never its key: each worker reads the key from
 * the container itself. Global teardown removes it, and a start that fails
 * before the record is written removes it at once.
 */
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { BazarrInstance, TimeBudget } from "./container";
import { allowStartup, readApiKey, REQUEST_MS, startBazarr } from "./container";

interface SharedRecord {
  name: string;
  baseURL: string;
}

const LOCK_POLL_MS = 1_000;
const LOCK_CAP_MS = 240_000;

export function runDir(): string {
  const dir = process.env.BAZARR_E2E_RUN_DIR;
  if (!dir)
    throw new Error("BAZARR_E2E_RUN_DIR is unset: global setup did not run");
  return dir;
}

const recordPath = () => join(runDir(), "shared-instance.json");
const lockPath = () => join(runDir(), "shared-instance.lock");

export async function readSharedRecord(): Promise<SharedRecord | null> {
  try {
    return JSON.parse(await readFile(recordPath(), "utf8")) as SharedRecord;
  } catch {
    return null;
  }
}

/** Marks setup as done through the API, as the wizard's last step does. */
export async function completeOnboarding(instance: BazarrInstance) {
  const form = new FormData();
  form.append("settings-general-setup_complete", "true");
  const response = await fetch(`${instance.baseURL}/api/system/settings`, {
    method: "POST",
    headers: { "X-API-KEY": instance.apiKey },
    body: form,
    signal: AbortSignal.timeout(REQUEST_MS),
  });
  if (!response.ok) {
    throw new Error(`completing onboarding answered ${response.status}`);
  }
}

async function acquireLock(): Promise<void> {
  const deadline = Date.now() + LOCK_CAP_MS;
  while (Date.now() < deadline) {
    try {
      await mkdir(lockPath());
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, LOCK_POLL_MS));
    }
  }
  throw new Error(
    "timed out waiting for another worker to start the shared instance",
  );
}

async function reuse(record: SharedRecord): Promise<BazarrInstance> {
  const apiKey = await readApiKey(record.name);
  // Stopped by global teardown, not by whichever worker finishes first.
  return { ...record, apiKey, stop: async () => undefined };
}

/**
 * The shared instance. A test that has to start it, or wait for another
 * worker to, gets the startup allowance on `budget`; a test that finds it
 * running keeps the suite's own timeout.
 */
export async function sharedBazarr(
  budget?: TimeBudget,
): Promise<BazarrInstance> {
  const running = await readSharedRecord();
  if (running) return reuse(running);
  if (budget) allowStartup(budget);
  await acquireLock();
  try {
    const existing = await readSharedRecord();
    if (existing) return reuse(existing);
    const instance = await startBazarr();
    try {
      await completeOnboarding(instance);
      const record: SharedRecord = {
        name: instance.name as string,
        baseURL: instance.baseURL,
      };
      await writeFile(recordPath(), JSON.stringify(record));
    } catch (error) {
      // Nothing else knows about it yet, so nothing else would remove it.
      await instance.stop();
      throw error;
    }
    return { ...instance, stop: async () => undefined };
  } finally {
    await rm(lockPath(), { recursive: true, force: true });
  }
}
