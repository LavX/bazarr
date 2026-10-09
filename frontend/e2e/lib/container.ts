/**
 * Starts and stops throwaway Bazarr+ containers for the end-to-end suite.
 *
 * Every container gets a fresh named volume for /config, so it boots as a new
 * install and leaves nothing behind once stop() has run. Both are named for
 * the run and for the attempt that made them, and labelled with the run, so
 * a start that fails removes only what it made itself and global teardown can
 * find anything a worker left behind. The API key is read from the
 * container's own configuration and kept in memory; it is never printed,
 * written to disk or attached to a report.
 */
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createServer } from "node:net";
import { basename } from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

export const DEFAULT_IMAGE = "bazarr-atlas:local";

/** Host ports tried for new containers, first free one wins. */
const PORT_RANGE = { first: 6790, last: 6849 };
/** Readiness is polled on this interval and given up on after the cap. */
const POLL_MS = 5_000;
const READY_CAP_MS = 180_000;
/** The most one readiness request may take, so the cap holds. */
const PROBE_MS = 10_000;
/** The most a docker command that reads a container may take. */
const DOCKER_MS = 15_000;
/** The most a one-off request to an instance may take. */
export const REQUEST_MS = 30_000;
/** Extra time a test gets when it has to wait for a container to boot. */
export const STARTUP_ALLOWANCE_MS = 200_000;
/**
 * The time removing a container and its volume may take. On a busy host
 * `docker rm -f -v` has taken well over a minute.
 */
export const REMOVAL_ALLOWANCE_MS = 180_000;
/** The label every container this module starts carries, for cleanup. */
export const E2E_LABEL = "bazarr-e2e";
/** The label that says which run a container or volume belongs to. */
export const E2E_RUN_LABEL = `${E2E_LABEL}-run`;

/** The run id of a process that did not come through global setup. */
const PROCESS_RUN = randomUUID().slice(0, 8);

/**
 * The id of this test run, the same in every worker: the name of the run
 * directory global setup made, less its prefix. Two runs on one machine
 * never share it, because the directories are made by mkdtemp.
 */
export function runId(): string {
  const dir = process.env.BAZARR_E2E_RUN_DIR;
  if (!dir) return PROCESS_RUN;
  const id = basename(dir)
    .replace(new RegExp(`^${E2E_LABEL}-`), "")
    .replace(/[^A-Za-z0-9_.-]/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "");
  return id || PROCESS_RUN;
}

export interface BazarrInstance {
  baseURL: string;
  apiKey: string;
  /** The container name, or null for an external instance. */
  name: string | null;
  stop(): Promise<void>;
}

/** The part of Playwright's TestInfo that holds a test's time budget. */
export interface TimeBudget {
  timeout: number;
  setTimeout(timeout: number): void;
}

/**
 * Gives a test `ms` more on top of its own time. A test that runs with no
 * timeout at all (0, as under --debug or --timeout 0) is left that way.
 */
export function allow(budget: TimeBudget, ms: number): void {
  if (budget.timeout > 0) budget.setTimeout(budget.timeout + ms);
}

/** Gives a test the time a container needs to boot, on top of its own. */
export function allowStartup(budget: TimeBudget): void {
  allow(budget, STARTUP_ALLOWANCE_MS);
}

/** How long readiness is polled for, how often, and how long one try may take. */
export interface ReadyOptions {
  capMs?: number;
  pollMs?: number;
  probeMs?: number;
}

export interface StartOptions {
  image?: string;
  port?: number;
}

/** Why the suite cannot start a container here, or null when it can. */
export async function dockerUnavailable(image: string): Promise<string | null> {
  try {
    await run("docker", ["version", "--format", "{{.Server.Version}}"]);
  } catch {
    return "docker is not available, so no Bazarr+ container can be started. Install docker or set BAZARR_E2E_URL to an instance you already run.";
  }
  try {
    await run("docker", ["image", "inspect", image, "--format", "{{.Id}}"]);
  } catch {
    return `the image ${image} is missing. Build it (see docs/agents/e2e.md) or set BAZARR_E2E_IMAGE to one that exists.`;
  }
  return null;
}

function portIsFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
  });
}

async function freePorts(): Promise<number[]> {
  const ports: number[] = [];
  for (let port = PORT_RANGE.first; port <= PORT_RANGE.last; port += 1) {
    if (await portIsFree(port)) ports.push(port);
  }
  return ports;
}

/** What is left of `limit` before the deadline, and never nothing. */
const within = (limit: number, deadline: number) =>
  Math.max(1, Math.min(limit, deadline - Date.now()));

async function status(url: string, timeout: number): Promise<number> {
  try {
    // A server that takes the connection and never answers would otherwise
    // hold the poll past its cap.
    const response = await fetch(url, {
      redirect: "manual",
      signal: AbortSignal.timeout(timeout),
    });
    return response.status;
  } catch {
    return 0;
  }
}

async function logsSayStarted(name: string, timeout: number): Promise<boolean> {
  try {
    const { stdout, stderr } = await run("docker", ["logs", name], {
      maxBuffer: 64 * 1024 * 1024,
      timeout,
    });
    return `${stdout}\n${stderr}`.includes("BAZARR is started");
  } catch {
    return false;
  }
}

/**
 * Waits until the app serves the wizard, the API answers and the log says the
 * backend finished starting. Also used after an in-app restart.
 */
export async function waitUntilReady(
  baseURL: string,
  name: string | null,
  {
    capMs = READY_CAP_MS,
    pollMs = POLL_MS,
    probeMs = PROBE_MS,
  }: ReadyOptions = {},
): Promise<void> {
  const deadline = Date.now() + capMs;
  let last = "";
  while (Date.now() < deadline) {
    const setup = await status(`${baseURL}/setup`, within(probeMs, deadline));
    const api = await status(
      `${baseURL}/api/system/settings`,
      within(probeMs, deadline),
    );
    const started =
      name === null
        ? true
        : await logsSayStarted(name, within(DOCKER_MS, deadline));
    last = `/setup ${setup}, /api/system/settings ${api}, started ${started}`;
    if (setup === 200 && (api === 200 || api === 401) && started) return;
    await new Promise((resolve) =>
      setTimeout(resolve, Math.max(0, Math.min(pollMs, deadline - Date.now()))),
    );
  }
  throw new Error(
    `Bazarr+ at ${baseURL} was not ready after ${capMs / 1000}s (${last})`,
  );
}

// Runs inside the container with the app's own decryption, so the key is
// decrypted where it lives and only crosses into this process's memory.
const READ_API_KEY = [
  "import sys, yaml",
  "sys.path.insert(0, '/app/bazarr/bazarr')",
  "from secret_store.crypto import decrypt_secret",
  "c = yaml.safe_load(open('/config/config/config.yaml'))",
  "sys.stdout.write(decrypt_secret(c['auth']['apikey'], c['general']['secrets_encryption_key']))",
].join("\n");

export async function readApiKey(name: string): Promise<string> {
  const { stdout } = await run(
    "docker",
    ["exec", name, "python", "-c", READ_API_KEY],
    { timeout: DOCKER_MS },
  );
  const key = stdout.trim();
  if (!key) throw new Error(`could not read the API key of ${name}`);
  return key;
}

async function remove(name: string): Promise<void> {
  await run("docker", ["rm", "-f", "-v", name]).catch(() => undefined);
  await run("docker", ["volume", "rm", "-f", name]).catch(() => undefined);
}

/**
 * Removes every container and volume labelled with this run, whoever made
 * them: what a worker that was killed, or that failed before it could clean
 * up, left behind. Containers and volumes of other runs are not touched.
 */
export async function removeRunLeftovers(): Promise<void> {
  const filter = `label=${E2E_RUN_LABEL}=${runId()}`;
  const list = (args: string[]) =>
    run("docker", args, { timeout: DOCKER_MS })
      .then(({ stdout }) => stdout.split(/\s+/).filter(Boolean))
      .catch(() => [] as string[]);
  const containers = await list(["ps", "-aq", "--filter", filter]);
  if (containers.length > 0) {
    await run("docker", ["rm", "-f", "-v", ...containers]).catch(
      () => undefined,
    );
  }
  const volumes = await list(["volume", "ls", "-q", "--filter", filter]);
  if (volumes.length > 0) {
    await run("docker", ["volume", "rm", "-f", ...volumes]).catch(
      () => undefined,
    );
  }
}

/**
 * Starts a fresh Bazarr+ on its own volume and resolves once it is ready.
 * Tries the next free port when docker loses a race for the first one.
 */
export async function startBazarr(
  options: StartOptions = {},
): Promise<BazarrInstance> {
  const image = options.image ?? process.env.BAZARR_E2E_IMAGE ?? DEFAULT_IMAGE;
  const candidates =
    options.port !== undefined ? [options.port] : await freePorts();
  const labels = [
    "--label",
    E2E_LABEL,
    "--label",
    `${E2E_RUN_LABEL}=${runId()}`,
  ];
  let lastError: unknown = new Error("no free port in the e2e range");
  for (const port of candidates) {
    // Unique to this attempt: another worker or run that picked the same
    // port gets a name of its own, so the cleanup below cannot reach it.
    const name = `${E2E_LABEL}-${runId()}-${port}-${randomUUID().slice(0, 8)}`;
    try {
      await run("docker", ["volume", "create", ...labels, name]);
      await run("docker", [
        "run",
        "--detach",
        "--name",
        name,
        ...labels,
        "--publish",
        `127.0.0.1:${port}:6767`,
        "--volume",
        `${name}:/config`,
        image,
      ]);
    } catch (error) {
      lastError = error;
      await remove(name);
      continue;
    }
    const baseURL = `http://127.0.0.1:${port}`;
    try {
      await waitUntilReady(baseURL, name);
      const apiKey = await readApiKey(name);
      return { baseURL, apiKey, name, stop: () => remove(name) };
    } catch (error) {
      await remove(name);
      throw error;
    }
  }
  throw lastError;
}

/** The container a worker keeps for the spec file it is running. */
export interface ContainerSlot {
  held: { file: string; instance: BazarrInstance } | null;
}

/**
 * The container for the spec file `file`: the one `slot` already holds for
 * it, or a fresh one in place of whatever it held before. The test that
 * replaces it gets the time to remove the old one and start the new one,
 * each before it begins.
 */
export async function containerForFile(
  slot: ContainerSlot,
  file: string,
  budget: TimeBudget,
  options: StartOptions = {},
): Promise<BazarrInstance> {
  if (slot.held?.file === file) return slot.held.instance;
  if (slot.held) {
    allow(budget, REMOVAL_ALLOWANCE_MS);
    await slot.held.instance.stop();
    slot.held = null;
  }
  allowStartup(budget);
  const instance = await startBazarr(options);
  slot.held = { file, instance };
  return instance;
}

/**
 * The base URL of an instance named by BAZARR_E2E_URL. Only the root of a
 * host is supported: specs open pages and call the API by absolute path, so
 * against an instance served under a path, such as /bazarr, they would reach
 * the host's root and test something else.
 */
export function externalBaseURL(url: string): string {
  const { pathname } = new URL(url);
  if (pathname !== "/") {
    throw new Error(
      `BAZARR_E2E_URL has the path ${pathname}, and the suite only runs against the root of a host. ` +
        "Its specs address every page and API call from the root, so they would miss an instance served under a sub-path. " +
        "Point it at an instance served at the root of its host or port.",
    );
  }
  return url.replace(/\/+$/, "");
}

/** An instance somebody else runs, named by BAZARR_E2E_URL. Never stopped. */
export async function externalBazarr(url: string): Promise<BazarrInstance> {
  const baseURL = externalBaseURL(url);
  let apiKey = process.env.BAZARR_E2E_API_KEY ?? "";
  if (!apiKey) {
    // An instance without authentication hands the key to its own page.
    const html = await fetch(`${baseURL}/`, {
      signal: AbortSignal.timeout(REQUEST_MS),
    }).then((r) => r.text());
    apiKey = /"apiKey":\s*"([^"]+)"/.exec(html)?.[1] ?? "";
  }
  return { baseURL, apiKey, name: null, stop: async () => undefined };
}

/** Removes a container this suite started, by name. */
export const removeContainer = remove;
