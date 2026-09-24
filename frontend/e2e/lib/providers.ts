/**
 * Gives an instance a working provider set: the recommended catalog providers
 * the wizard offers, installed through the Provider Hub API, enabled, and
 * loaded by a restart.
 *
 * Specs that search the provider network call ensureRecommendedProviders in a
 * beforeEach. The first call on an instance does the work and the rest find it
 * done, so a file pays for the install once. Stateless specs share one
 * instance, and a lock in the run directory keeps two workers from installing
 * into it at the same time.
 */
import type { APIRequestContext, TestInfo } from "@playwright/test";
import { execFile } from "node:child_process";
import { mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import type { BazarrInstance } from "./container";
import { waitUntilReady } from "./container";
import { runDir } from "./shared";

const run = promisify(execFile);

const POLL_MS = 2_000;
/** Installs, the enable and the restart, all under this cap. */
const INSTALL_CAP_MS = 300_000;
const RESTART_CAP_MS = 180_000;
/** Extra test time for the call that has to install or wait for another worker. */
const INSTALL_ALLOWANCE_MS = INSTALL_CAP_MS + RESTART_CAP_MS;

const CATALOG_REPO = "https://github.com/LavX/bazarr-provider-catalog";
const OFFICIAL_SOURCE = "official";
/** A catalog read can fetch the catalog from GitHub before it answers. */
const CATALOG_TIMEOUT_MS = 60_000;

type Loose = Record<string, unknown>;

interface CatalogEntry {
  provider_id: string;
  name?: string;
  manifest?: Loose | string;
}

interface Installation {
  provider_id: string;
  state?: string;
  pending_restart?: boolean;
}

function manifestOf(entry: CatalogEntry): Loose | null {
  const raw = entry.manifest;
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw) as Loose;
    } catch {
      return null;
    }
  }
  return raw && typeof raw === "object" ? raw : null;
}

function schemaKeys(manifest: Loose): Record<string, Loose> {
  const schema = manifest.config_schema as Loose | undefined;
  return (schema?.properties as Record<string, Loose> | undefined) ?? {};
}

function hasCapability(manifest: Loose, flag: string, field: RegExp) {
  if (typeof manifest[flag] === "boolean") return manifest[flag] as boolean;
  return Object.keys(schemaKeys(manifest)).some((key) => field.test(key));
}

const SIGNUP_KEYS = new Set([
  "username",
  "password",
  "email",
  "passkey",
  "token",
  "api_key",
  "apikey",
  "cookies",
]);

/**
 * The wizard's "Install recommended" rule, read off the same manifest fields:
 * no signup, nothing required, and no FlareSolverr or captcha helper. Kept in
 * step with src/pages/Setup/steps/providers/recommended.ts.
 */
export function isRecommended(entry: CatalogEntry): boolean {
  const manifest = manifestOf(entry);
  if (!manifest) return false;
  const text = `${entry.provider_id} ${entry.name ?? ""} ${String(manifest.description ?? "")}`;
  if (/smoketest/i.test(text)) return false;
  const signup = Object.entries(schemaKeys(manifest)).some(
    ([key, spec]) =>
      spec?.type !== "boolean" && SIGNUP_KEYS.has(key.toLowerCase()),
  );
  if (signup) return false;
  const required = (manifest.config_schema as Loose | undefined)?.required;
  if (Array.isArray(required) && required.length > 0) return false;
  return (
    !hasCapability(manifest, "flaresolverr", /flaresolverr/i) &&
    !hasCapability(manifest, "anti_captcha", /captcha/i)
  );
}

async function getJson<T>(
  api: APIRequestContext,
  path: string,
  timeout?: number,
): Promise<T> {
  const response = await api.get(path, { timeout });
  if (!response.ok()) {
    throw new Error(`GET ${path} answered ${response.status()}`);
  }
  return (await response.json()) as T;
}

async function installations(api: APIRequestContext): Promise<Installation[]> {
  return (
    await getJson<{ data: Installation[] }>(api, "/api/provider-hub/providers")
  ).data;
}

/**
 * The catalog entries. On a shared build machine GitHub's API quota for the
 * address can run out, and the catalog then comes back empty because the
 * branch could not be turned into a commit. The commit is then looked up with
 * git, which does not use that quota, and the official source is pointed at
 * it: the same catalog, fetched from the same place.
 */
async function catalogEntries(api: APIRequestContext): Promise<CatalogEntry[]> {
  const catalog = await getJson<{ entries: CatalogEntry[] }>(
    api,
    "/api/provider-hub/catalog",
    CATALOG_TIMEOUT_MS,
  );
  if (catalog.entries.length > 0) return catalog.entries;
  const { stdout } = await run("git", ["ls-remote", CATALOG_REPO, "main"]);
  const commit = stdout.split(/\s/)[0];
  if (!/^[0-9a-f]{40}$/.test(commit)) {
    throw new Error("could not resolve the provider catalog's main commit");
  }
  const response = await api.patch(
    `/api/provider-hub/catalog/sources/${OFFICIAL_SOURCE}`,
    // The field name is the API's own.
    // eslint-disable-next-line camelcase
    { data: { dev_ref: commit }, timeout: CATALOG_TIMEOUT_MS },
  );
  if (!response.ok()) {
    throw new Error(`pinning the catalog answered ${response.status()}`);
  }
  const pinned = await getJson<{ entries: CatalogEntry[] }>(
    api,
    "/api/provider-hub/catalog",
    CATALOG_TIMEOUT_MS,
  );
  if (pinned.entries.length === 0) {
    throw new Error("the provider catalog is empty");
  }
  return pinned.entries;
}

/**
 * Waits until none of the jobs is pending or running. The queue keeps only
 * the last few finished jobs, so a job that is no longer listed has finished.
 */
async function waitForJobs(api: APIRequestContext, ids: number[]) {
  const deadline = Date.now() + INSTALL_CAP_MS;
  const wanted = new Set(ids);
  while (Date.now() < deadline) {
    const open = (
      await getJson<{ data: { job_id: number; status: string }[] }>(
        api,
        "/api/system/jobs",
      )
    ).data.filter(
      (job) =>
        wanted.has(job.job_id) &&
        (job.status === "pending" || job.status === "running"),
    );
    if (open.length === 0) return;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  throw new Error(
    `provider installs had not finished after ${INSTALL_CAP_MS / 1000}s`,
  );
}

async function enable(api: APIRequestContext, ids: string[]) {
  const settings = await getJson<{ general: { enabled_providers?: string[] } }>(
    api,
    "/api/system/settings",
  );
  const current = settings.general.enabled_providers ?? [];
  const wanted = Array.from(new Set([...current, ...ids]));
  if (wanted.length === current.length) return;
  const form = new FormData();
  for (const id of wanted)
    form.append("settings-general-enabled_providers", id);
  const response = await api.post("/api/system/settings", { multipart: form });
  if (!response.ok()) {
    throw new Error(`enabling providers answered ${response.status()}`);
  }
}

/** Restarts the backend and waits until every staged provider is loaded. */
async function restartIntoPlace(
  api: APIRequestContext,
  bazarr: BazarrInstance,
) {
  // The connection can drop before the answer arrives; the poll below is
  // what says whether the restart happened.
  await api
    .post("/api/system", { form: { action: "restart" } })
    .catch(() => undefined);
  const deadline = Date.now() + RESTART_CAP_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    const loaded = await installations(api)
      .then((rows) => rows.every((row) => !row.pending_restart))
      .catch(() => false);
    if (loaded) {
      await waitUntilReady(bazarr.baseURL, bazarr.name);
      return;
    }
  }
  throw new Error(
    `staged providers were not loaded ${RESTART_CAP_MS / 1000}s after the restart`,
  );
}

async function withLock<T>(bazarr: BazarrInstance, work: () => Promise<T>) {
  const key = (bazarr.name ?? new URL(bazarr.baseURL).host).replace(/\W/g, "-");
  const lock = join(runDir(), `providers-${key}.lock`);
  const deadline = Date.now() + INSTALL_ALLOWANCE_MS;
  for (;;) {
    try {
      await mkdir(lock);
      break;
    } catch {
      if (Date.now() > deadline) {
        throw new Error(
          "timed out waiting for another worker's provider install",
        );
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
  }
  try {
    return await work();
  } finally {
    await rm(lock, { recursive: true, force: true });
  }
}

async function activeRecommended(
  api: APIRequestContext,
  recommended: string[],
): Promise<string[]> {
  const active = new Set(
    (await installations(api))
      .filter((row) => row.state === "active" && !row.pending_restart)
      .map((row) => row.provider_id),
  );
  return recommended.filter((id) => active.has(id));
}

/**
 * Installs, enables and loads the recommended providers, or reuses them when
 * an earlier call already did. Resolves with the ids that are active.
 */
export async function ensureRecommendedProviders(
  api: APIRequestContext,
  bazarr: BazarrInstance,
  testInfo: TestInfo,
): Promise<string[]> {
  const active = (await installations(api)).filter(
    (row) => row.state === "active" && !row.pending_restart,
  );
  if (active.length === 0) {
    testInfo.setTimeout(testInfo.timeout + INSTALL_ALLOWANCE_MS);
  }
  return withLock(bazarr, async () => {
    const entries = new Map(
      (await catalogEntries(api))
        .filter(isRecommended)
        .map((entry) => [entry.provider_id, entry]),
    );
    const recommended = Array.from(entries.keys());
    const present = new Set(
      (await installations(api)).map((row) => row.provider_id),
    );
    const missing = recommended.filter((id) => !present.has(id));
    if (missing.length > 0) {
      const jobs: number[] = [];
      for (const id of missing) {
        const response = await api.post("/api/provider-hub/installations", {
          data: { manifest: manifestOf(entries.get(id) as CatalogEntry) },
        });
        if (response.status() !== 202) {
          throw new Error(`installing ${id} answered ${response.status()}`);
        }
        jobs.push(((await response.json()) as { job_id: number }).job_id);
      }
      await waitForJobs(api, jobs);
    }
    const installed = new Set(
      (await installations(api)).map((row) => row.provider_id),
    );
    await enable(
      api,
      recommended.filter((id) => installed.has(id)),
    );
    if ((await installations(api)).some((row) => row.pending_restart)) {
      await restartIntoPlace(api, bazarr);
    }
    const ready = await activeRecommended(api, recommended);
    if (ready.length === 0) {
      throw new Error("no recommended provider is active after the install");
    }
    return ready;
  });
}
