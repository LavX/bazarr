/**
 * Starts and stops throwaway Bazarr+ containers for the end-to-end suite.
 *
 * Every container gets a fresh named volume for /config, so it boots as a new
 * install and leaves nothing behind once stop() has run. The API key is read
 * from the container's own configuration and kept in memory; it is never
 * printed, written to disk or attached to a report.
 */
import { execFile } from "node:child_process";
import { createServer } from "node:net";
import { promisify } from "node:util";

const run = promisify(execFile);

export const DEFAULT_IMAGE = "bazarr-atlas:local";

/** Host ports tried for new containers, first free one wins. */
const PORT_RANGE = { first: 6790, last: 6849 };
/** Readiness is polled on this interval and given up on after the cap. */
const POLL_MS = 5_000;
const READY_CAP_MS = 180_000;
/** The label every container this module starts carries, for cleanup. */
export const E2E_LABEL = "bazarr-e2e";

export interface BazarrInstance {
  baseURL: string;
  apiKey: string;
  /** The container name, or null for an external instance. */
  name: string | null;
  stop(): Promise<void>;
}

export interface StartOptions {
  image?: string;
  port?: number;
  name?: string;
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

async function status(url: string): Promise<number> {
  try {
    const response = await fetch(url, { redirect: "manual" });
    return response.status;
  } catch {
    return 0;
  }
}

async function logsSayStarted(name: string): Promise<boolean> {
  try {
    const { stdout, stderr } = await run("docker", ["logs", name], {
      maxBuffer: 64 * 1024 * 1024,
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
): Promise<void> {
  const deadline = Date.now() + READY_CAP_MS;
  let last = "";
  while (Date.now() < deadline) {
    const setup = await status(`${baseURL}/setup`);
    const api = await status(`${baseURL}/api/system/settings`);
    const started = name === null ? true : await logsSayStarted(name);
    last = `/setup ${setup}, /api/system/settings ${api}, started ${started}`;
    if (setup === 200 && (api === 200 || api === 401) && started) return;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  throw new Error(
    `Bazarr+ at ${baseURL} was not ready after ${READY_CAP_MS / 1000}s (${last})`,
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
  const { stdout } = await run("docker", [
    "exec",
    name,
    "python",
    "-c",
    READ_API_KEY,
  ]);
  const key = stdout.trim();
  if (!key) throw new Error(`could not read the API key of ${name}`);
  return key;
}

async function remove(name: string): Promise<void> {
  await run("docker", ["rm", "-f", "-v", name]).catch(() => undefined);
  await run("docker", ["volume", "rm", "-f", name]).catch(() => undefined);
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
  let lastError: unknown = new Error("no free port in the e2e range");
  for (const port of candidates) {
    const name = options.name ?? `${E2E_LABEL}-${port}`;
    try {
      await run("docker", [
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        E2E_LABEL,
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

/** An instance somebody else runs, named by BAZARR_E2E_URL. Never stopped. */
export async function externalBazarr(url: string): Promise<BazarrInstance> {
  const baseURL = url.replace(/\/+$/, "");
  let apiKey = process.env.BAZARR_E2E_API_KEY ?? "";
  if (!apiKey) {
    // An instance without authentication hands the key to its own page.
    const html = await fetch(`${baseURL}/`).then((r) => r.text());
    apiKey = /"apiKey":\s*"([^"]+)"/.exec(html)?.[1] ?? "";
  }
  return { baseURL, apiKey, name: null, stop: async () => undefined };
}

/** Removes a container this suite started, by name. */
export const removeContainer = remove;
