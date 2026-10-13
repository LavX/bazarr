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
 *
 * With BAZARR_E2E_BASE_PATH set, every container this module starts is first
 * made to serve under that path, as `general.base_url`, and is handed out
 * with a baseURL that carries it (lib/base.ts).
 */
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createServer } from "node:net";
import { basename } from "node:path";
import { promisify } from "node:util";
import { basePath } from "./base";

const run = promisify(execFile);

export const DEFAULT_IMAGE = "bazarr-atlas:local";

/** Host ports tried for new containers, first free one wins. */
const PORT_RANGE = { first: 6790, last: 6849 };
/** Readiness is polled on this interval and given up on after the cap. */
export const POLL_MS = 5_000;
/** The time one readiness phase may take: the boot, or the restart a base path applies. */
export const READY_CAP_MS = 180_000;
/** The most one readiness request may take, so the cap holds. */
const PROBE_MS = 10_000;
/**
 * The most a docker command of a start may take: the calls that create
 * the container, and the reads of one. A busy host can hold a docker
 * call the better part of a minute, and its removals have taken well
 * over one (see REMOVAL_ALLOWANCE_MS), so the bound is a minute rather
 * than the seconds a call takes at rest: a bound that kills healthy
 * calls fails starts that were about to succeed.
 */
export const DOCKER_MS = 60_000;
/** The most restarting a container may take: a stop and a start, not the boot. */
export const RESTART_MS = 60_000;
/** The most a one-off request to an instance may take. */
export const REQUEST_MS = 30_000;
/**
 * The time removing a container and its volume may take, as one stage:
 * the bound remove() gives its own two docker calls, the allowance a
 * test that waits for a removal gets, and the retry term the startup
 * allowance counts for each lost port race. On a busy host
 * `docker rm -f -v` has taken well over a minute.
 */
export const REMOVAL_ALLOWANCE_MS = 180_000;
/**
 * The most lost races for a port a start retries before it gives up,
 * and what each retry adds to the startup allowance: the two docker
 * calls that made the attempt, and the removal of what they left.
 */
export const PORT_RETRIES = 4;
/**
 * Every other bounded stage of a start, as the sum of their bounds: the
 * two docker calls that create the container and the key read (the docker
 * bound each), the save of the base, the three verification requests and
 * the onboarding (the request bound each), the restart that applies
 * the base, and the lost port races the start retries, each at the two
 * docker calls and the removal it costs.
 */
export const STARTUP_STAGES_MS =
  2 * DOCKER_MS + // the docker calls that create the container
  DOCKER_MS + // the key read
  REQUEST_MS + // the save of the base
  RESTART_MS + // the restart that applies it
  3 * REQUEST_MS + // the verification requests
  REQUEST_MS + // the onboarding of the shared instance
  PORT_RETRIES * (2 * DOCKER_MS + REMOVAL_ALLOWANCE_MS); // the lost races for a port
/**
 * The margin on top of the readiness caps, for every other bounded stage
 * of a start: exactly the sum of those stages' bounds, so a start in
 * which every stage takes its whole time still fits the one test's
 * budget. The harness's check fails the suite if this is cut below the
 * sum.
 */
const STARTUP_SLACK_MS = STARTUP_STAGES_MS;
/**
 * The readiness phases one start waits through: the boot, and the restart
 * that applies a base path, each with its own cap.
 */
function readinessPhases(): number {
  return basePath() === "" ? 1 : 2;
}
/**
 * Extra time a test gets when it has to wait for a container to come up.
 * One readiness cap per phase the start waits through, plus the sum of
 * the bounds of every other bounded stage a start runs: the container's
 * creation, the key read, the save of the base, the restart that applies
 * it, the verification requests, the shared instance's onboarding, and
 * the lost port races the start retries. A base path adds the second
 * phase, and the allowance covers the whole start: one in which every
 * stage takes its whole time, every race it retries included, still
 * fits the test's budget.
 */
export function startupAllowanceMs(): number {
  return readinessPhases() * READY_CAP_MS + STARTUP_SLACK_MS;
}

/** Gives a test the time a container needs to boot, on top of its own. */
export function allowStartup(budget: TimeBudget): void {
  allow(budget, startupAllowanceMs());
}

/** The label every container this module starts carries, for cleanup. */
export const E2E_LABEL = "bazarr-e2e";
/** The label that says which run a container or volume belongs to. */
export const E2E_RUN_LABEL = `${E2E_LABEL}-run`;
/** The throwaway Postgres image of the PostgreSQL variant, overridable. */
const DEFAULT_POSTGRES_IMAGE = "postgres:16-alpine";
/** How long the throwaway Postgres is given to accept connections. */
const POSTGRES_READY_CAP_MS = 60_000;
const POSTGRES_POLL_MS = 2_000;
/** The credentials the throwaway Postgres and the instance share. */
const POSTGRES_USER = "bazarr";
const POSTGRES_DATABASE = "bazarr";

/**
 * Whether this run boots every instance against a throwaway Postgres, selected
 * by the environment. With it on, each Bazarr+ container gets a Postgres of
 * its own on a network of its own, and onboarding proves the write path.
 */
export function postgresVariant(): boolean {
  return process.env.BAZARR_E2E_POSTGRES === "1";
}

function postgresImage(): string {
  return process.env.BAZARR_E2E_POSTGRES_IMAGE ?? DEFAULT_POSTGRES_IMAGE;
}

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

/** How long readiness is polled for, how often, and how long one try may take. */
export interface ReadyOptions {
  capMs?: number;
  pollMs?: number;
  probeMs?: number;
}

export interface StartOptions {
  image?: string;
  port?: number;
  /** Test seam: how long the variant's Postgres is given to accept connections. */
  postgresReadyCapMs?: number;
}

/**
 * The docker id of `image`, or null when there is none to read: docker is
 * down, or the image is missing.
 */
export async function imageId(image: string): Promise<string | null> {
  try {
    const { stdout } = await run(
      "docker",
      ["image", "inspect", image, "--format", "{{.Id}}"],
      { timeout: DOCKER_MS },
    );
    const id = stdout.trim();
    return id === "" ? null : id;
  } catch {
    return null;
  }
}

/** Why the suite cannot start a container here, or null when it can. */
export async function dockerUnavailable(image: string): Promise<string | null> {
  try {
    await run("docker", ["version", "--format", "{{.Server.Version}}"]);
  } catch {
    return "docker is not available, so no Bazarr+ container can be started. Install docker or set BAZARR_E2E_URL to an instance you already run.";
  }
  if ((await imageId(image)) === null) {
    return `the image ${image} is missing. Build it (see docs/agents/e2e.md) or set BAZARR_E2E_IMAGE to one that exists.`;
  }
  if (postgresVariant()) {
    try {
      await run("docker", [
        "image",
        "inspect",
        postgresImage(),
        "--format",
        "{{.Id}}",
      ]);
    } catch {
      return `the image ${postgresImage()} is missing, and the PostgreSQL variant needs it. Pull it or set BAZARR_E2E_POSTGRES_IMAGE to one that exists.`;
    }
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

/**
 * This worker's place in the port scan, as the parallel index Playwright
 * gives the worker: workers that start at once and all scan from the
 * same first port would race one another for it, so each scans from its
 * own place instead.
 */
function parallelIndex(): number {
  const index = Number.parseInt(process.env.TEST_PARALLEL_INDEX ?? "", 10);
  return Number.isInteger(index) && index > 0 ? index : 0;
}

async function freePorts(): Promise<number[]> {
  const count = PORT_RANGE.last - PORT_RANGE.first + 1;
  const offset = parallelIndex() % count;
  const ports: number[] = [];
  // The scan starts at the worker's own place in the range and wraps,
  // so the offset costs the start nothing: the same ports are the
  // candidates, only in another order, and the first free one still wins.
  for (let step = 0; step < count; step += 1) {
    const port = PORT_RANGE.first + ((offset + step) % count);
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

/**
 * Removes a container and its volume, bounded as one stage: both calls
 * share the removal allowance, so a removal held on a busy host gives up
 * on what is left of it instead of holding the start or the teardown
 * that waits for it past every budget that counts the allowance.
 */
async function remove(name: string): Promise<void> {
  const deadline = Date.now() + REMOVAL_ALLOWANCE_MS;
  await run("docker", ["rm", "-f", "-v", name], {
    timeout: within(REMOVAL_ALLOWANCE_MS, deadline),
  }).catch(() => undefined);
  await run("docker", ["volume", "rm", "-f", name], {
    timeout: within(REMOVAL_ALLOWANCE_MS, deadline),
  }).catch(() => undefined);
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
  // The PostgreSQL variant also leaves a network per instance behind.
  const networks = await list(["network", "ls", "-q", "--filter", filter]);
  if (networks.length > 0) {
    await run("docker", ["network", "rm", ...networks]).catch(() => undefined);
  }
}

/**
 * Answers whether the throwaway Postgres accepts connections yet, bounded to
 * one short probe per call.
 */
async function postgresAccepts(
  name: string,
  timeout: number,
): Promise<boolean> {
  try {
    await run(
      "docker",
      [
        "exec",
        name,
        "pg_isready",
        // TCP, not the unix socket: during initdb the image runs a
        // temporary server on the socket only, which would answer ready.
        "-h",
        "127.0.0.1",
        "-U",
        POSTGRES_USER,
        "-d",
        POSTGRES_DATABASE,
      ],
      { timeout },
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * The Postgres container of a network, whose name is derived from it. `stop`
 * removes this name whenever the network is set, so a container that started
 * and then failed its readiness wait is never left behind by an assignment
 * that never happened.
 */
function postgresOf(network: string): string {
  return `${network}-postgres`;
}

/**
 * Starts the throwaway Postgres of the PostgreSQL variant on `network`, and
 * resolves once it accepts connections. Both containers share the run's
 * labels, so cleanup finds them whoever left them behind.
 */
async function startPostgres(
  network: string,
  labels: string[],
  capMs: number,
): Promise<{ name: string; password: string }> {
  const name = postgresOf(network);
  const password = randomUUID();
  await run("docker", ["network", "create", ...labels, network]);
  await run("docker", [
    "run",
    "--detach",
    "--name",
    name,
    ...labels,
    "--network",
    network,
    // The image declares VOLUME /var/lib/postgresql/data, so without this
    // every run would also leave an anonymous unlabelled volume behind.
    // A tmpfs also skips initdb's fsync phase, so it starts faster.
    "--tmpfs",
    "/var/lib/postgresql/data",
    "--env",
    `POSTGRES_USER=${POSTGRES_USER}`,
    "--env",
    `POSTGRES_PASSWORD=${password}`,
    "--env",
    `POSTGRES_DB=${POSTGRES_DATABASE}`,
    postgresImage(),
  ]);
  const deadline = Date.now() + capMs;
  while (!(await postgresAccepts(name, within(DOCKER_MS, deadline)))) {
    if (Date.now() >= deadline) {
      // The caller's stop() removes this container through the network's
      // own name for it, so a readiness failure leaks nothing.
      throw new Error(
        `the Postgres ${name} was not ready after ${capMs / 1000}s`,
      );
    }
    await new Promise((resolve) =>
      setTimeout(
        resolve,
        Math.max(0, Math.min(POSTGRES_POLL_MS, deadline - Date.now())),
      ),
    );
  }
  return { name, password };
}

/** Removes a docker network this suite created, if it still exists. */
async function removeNetwork(network: string): Promise<void> {
  await run("docker", ["network", "rm", network]).catch(() => undefined);
}

/**
 * Saves `base` as the instance's `general.base_url` and restarts the
 * container, and resolves once the instance answers under it. The restart is
 * of the whole container, not one asked for through the app's own API: the
 * supervisor in front of the app builds the proxy routes for the base from
 * this same setting at its own boot, so an in-app restart leaves the app's
 * API reachable only at the root, where nothing under the base answers
 * anything but GET. The readiness poll is what says whether the restart
 * happened.
 */
async function serveUnderBaseURL(
  rootURL: string,
  name: string,
  apiKey: string,
  base: string,
): Promise<string> {
  const headers = { "X-API-KEY": apiKey };
  const form = new FormData();
  form.append("settings-general-base_url", base);
  const saved = await fetch(`${rootURL}/api/system/settings`, {
    method: "POST",
    headers,
    body: form,
    signal: AbortSignal.timeout(REQUEST_MS),
  });
  if (!saved.ok) {
    throw new Error(`saving general.base_url=${base} answered ${saved.status}`);
  }
  await run("docker", ["restart", name], { timeout: RESTART_MS });
  const baseURL = `${rootURL}${base}`;
  await waitUntilReady(baseURL, name);
  // Readiness alone does not prove the base took effect: an app served at
  // the root answers /bazarr/setup too, through its catch-all, and so does
  // an old process that has not exited yet. Make the instance prove it: the
  // settings it serves under the base say the base, the supervisor's own
  // status route answers there, and the root no longer answers the API.
  const settings = await fetch(`${baseURL}/api/system/settings`, {
    headers,
    redirect: "manual",
    signal: AbortSignal.timeout(REQUEST_MS),
  });
  const settingsType = settings.headers.get("content-type") ?? "";
  if (!settings.ok || !settingsType.includes("json")) {
    // A save that was accepted but ignored leaves the app's front page,
    // HTML, answering under the base, not the settings API.
    throw new Error(
      `the settings under ${base} answered ${settings.status} (${settingsType || "no content type"}), so the base path did not take effect`,
    );
  }
  const reported = (await settings.json()) as {
    general?: { base_url?: string };
  };
  if (reported.general?.base_url !== base) {
    throw new Error(
      `the instance reports general.base_url=${JSON.stringify(reported.general?.base_url)}, not ${base}`,
    );
  }
  // The supervisor registers its status route under the base only when it
  // booted with it; without that its catch-all serves the SPA, HTML.
  const supervisor = await fetch(`${baseURL}/_supervisor/status`, {
    headers,
    redirect: "manual",
    signal: AbortSignal.timeout(REQUEST_MS),
  });
  const supervisorType = supervisor.headers.get("content-type") ?? "";
  if (!supervisor.ok || !supervisorType.includes("json")) {
    throw new Error(
      `the supervisor status under ${base} answered ${supervisor.status} (${supervisorType || "no content type"}), so the base path did not take effect`,
    );
  }
  const root = await fetch(`${rootURL}/api/system/status`, {
    headers,
    redirect: "manual",
    signal: AbortSignal.timeout(REQUEST_MS),
  });
  const stillJson =
    root.ok && (root.headers.get("content-type") ?? "").includes("json");
  if (stillJson) {
    throw new Error(
      `${rootURL} still answers the API, so the base path ${base} did not take effect`,
    );
  }
  return baseURL;
}

/**
 * Whether docker lost the race for the port, the one failure the next
 * port retries, up to PORT_RETRIES races: another worker or run took
 * the port between the scan and the run. Docker leaves a created,
 * stopped container behind, which the caller removes before trying
 * again.
 */
function isPortAllocationError(error: unknown): boolean {
  const text = `${error instanceof Error ? error.message : String(error)}\n${
    (error as { stderr?: string }).stderr ?? ""
  }`;
  return (
    text.includes("port is already allocated") ||
    text.includes("address already in use")
  );
}

/**
 * Starts a fresh Bazarr+ on its own volume and resolves once it is ready.
 * Tries the next free port when docker loses a race for one; any other
 * failure, a docker call killed on a busy host included, fails the start
 * and removes what it made.
 *
 * With the PostgreSQL variant on, each instance also gets a throwaway
 * Postgres on a network of its own, and boots pointed at it through the
 * environment; both go away with the instance.
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
  let collisions = 0;
  for (const port of candidates) {
    // Unique to this attempt: another worker or run that picked the same
    // port gets a name of its own, so the cleanup below cannot reach it.
    const name = `${E2E_LABEL}-${runId()}-${port}-${randomUUID().slice(0, 8)}`;
    const network = postgresVariant() ? `${name}-net` : null;
    let postgres: { name: string; password: string } | null = null;
    // The Postgres container is removed by the network's own name for it,
    // never by an assignment, so a start that failed at any point after its
    // creation still leaves stop() able to reach it.
    const stop = async () => {
      await remove(name);
      if (network) {
        await remove(postgresOf(network));
        await removeNetwork(network);
      }
    };
    try {
      await run("docker", ["volume", "create", ...labels, name], {
        timeout: DOCKER_MS,
      });
      if (network)
        postgres = await startPostgres(
          network,
          labels,
          options.postgresReadyCapMs ?? POSTGRES_READY_CAP_MS,
        );
    } catch (error) {
      // A volume or a database that would not start is the same on every
      // port, so it is cleaned up and reported rather than retried sixty
      // times over.
      await stop();
      throw error;
    }
    try {
      await run(
        "docker",
        [
          "run",
          "--detach",
          "--name",
          name,
          ...labels,
          ...(network && postgres
            ? [
                "--network",
                network,
                "--env",
                "POSTGRES_ENABLED=true",
                "--env",
                `POSTGRES_HOST=${postgres.name}`,
                "--env",
                "POSTGRES_PORT=5432",
                "--env",
                `POSTGRES_USERNAME=${POSTGRES_USER}`,
                "--env",
                `POSTGRES_PASSWORD=${postgres.password}`,
                "--env",
                `POSTGRES_DATABASE=${POSTGRES_DATABASE}`,
              ]
            : []),
          "--publish",
          `127.0.0.1:${port}:6767`,
          "--volume",
          `${name}:/config`,
          image,
        ],
        { timeout: DOCKER_MS },
      );
    } catch (error) {
      // Only a lost race for the port is worth the next port. Anything
      // else, a docker call killed past its bound included, is this
      // start's own failure: walking on would pay a fresh volume and
      // container for every port in the range, so it removes what it
      // made and fails with the error it got. The stop also takes the
      // throwaway Postgres and its network when the variant is on.
      if (!isPortAllocationError(error)) {
        await stop();
        throw error;
      }
      lastError = error;
      await stop();
      // PORT_RETRIES lost races are the whole headroom the startup
      // allowance counts, so a race past them fails the start with the
      // error it lost to instead of holding its test past every budget
      // that counts the allowance.
      collisions += 1;
      if (collisions > PORT_RETRIES) throw lastError;
      continue;
    }
    const baseURL = `http://127.0.0.1:${port}`;
    try {
      await waitUntilReady(baseURL, name);
      const apiKey = await readApiKey(name);
      const base = basePath();
      const under =
        base === ""
          ? baseURL
          : await serveUnderBaseURL(baseURL, name, apiKey, base);
      return {
        baseURL: under,
        apiKey,
        name,
        stop,
      };
    } catch (error) {
      await stop();
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
 * The base URL of an instance named by BAZARR_E2E_URL, its path included: an
 * instance served under a sub-path, such as /bazarr, is accepted, and the
 * suite resolves every page and API path against that path through
 * appPath() in lib/base.ts. A URL that is not a URL fails here, with the
 * TypeError of Node's own URL parser, before anything runs against it.
 */
export function externalBaseURL(url: string): string {
  new URL(url);
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
    if (!apiKey && /window\.Bazarr\s*=\s*JSON\.parse/.test(html)) {
      // The shell Bazarr serves boots from window.Bazarr, which carries the
      // API key only for a session the instance lets in. A shell without
      // the key opens on the login form.
      throw new Error(
        `The instance at ${baseURL} asks for a login, so its page hands out no API key. ` +
          "Set BAZARR_E2E_API_KEY: without it the specs that use the api fixture send an " +
          "empty key and answer 401, while the browser specs still pass on the session " +
          "cookie, so the failure reads like a suite regression when it is a missing variable.",
      );
    }
  }
  return { baseURL, apiKey, name: null, stop: async () => undefined };
}

/** Removes a container this suite started, by name. */
export const removeContainer = remove;
