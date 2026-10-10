/* eslint-disable camelcase -- API bodies keep their transport field names. */
/**
 * The e2e harness's own checks, run by vitest in plain Node (the
 * `e2e-harness` project in vite.config.ts). Docker is an in-memory stand-in
 * and the network a stubbed fetch, so nothing here starts a container or
 * needs one.
 */
import globalTeardown from "@e2e/global-teardown";
import type { APIRequestContext, TestInfo } from "@playwright/test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import type { AddressInfo, Socket } from "node:net";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BazarrInstance, ContainerSlot } from "./container";
import {
  containerForFile,
  DOCKER_MS,
  E2E_LABEL,
  E2E_RUN_LABEL,
  externalBazarr,
  POLL_MS,
  PORT_RETRIES,
  postgresVariant,
  READY_CAP_MS,
  REMOVAL_ALLOWANCE_MS,
  REQUEST_MS,
  RESTART_MS,
  runId,
  startBazarr,
  STARTUP_STAGES_MS,
  startupAllowanceMs,
  waitUntilReady,
} from "./container";
import { ensureRecommendedProviders, INSTALL_ALLOWANCE_MS } from "./providers";
import { readSharedRecord, sharedBazarr } from "./shared";

/**
 * A real event loop turn, held before any test replaces the timers with fake
 * ones. The fake clock only moves where a test drives it, and a real turn is
 * how the real filesystem the lock and the record live on gets to answer
 * while the clock is being driven.
 */
const realTimeout = setTimeout;

interface FakeContainer {
  name: string;
  port: number | undefined;
  labels: string[];
  volume: string;
  network?: string;
  env?: string[];
  running: boolean;
}

interface DockerCall {
  args: string[];
  timeout?: number;
  /** When the call happened, on the same clock as the stubbed fetch's calls. */
  seq: number;
}

/**
 * Just enough of docker for the harness: names and host ports are unique the
 * way the daemon keeps them, and a run that loses a port leaves a created,
 * stopped container behind, as `docker run` does.
 */
const docker = vi.hoisted(() => {
  const containers = new Map<string, FakeContainer>();
  const volumes = new Map<string, string[]>();
  const networks = new Map<string, string[]>();
  const calls: DockerCall[] = [];
  const state = {
    containers,
    volumes,
    networks,
    calls,
    /** The shared clock of the stubbed docker and the stubbed fetch. */
    clock: 0,
    /** Runs at the start of each `docker run`, to let another run win. */
    beforeRun: null as ((port: number) => void) | null,
    /** How long a docker call takes, on the same clock as everything else. */
    delay: null as ((args: string[]) => number) | null,
    /** Makes every pg_isready probe fail, as an unreachable Postgres does. */
    pgNeverReady: false,
    /** Makes every Bazarr run fail with this message, as a missing image does. */
    bazarrRunFails: "" as string,
    reset() {
      containers.clear();
      volumes.clear();
      networks.clear();
      calls.length = 0;
      state.beforeRun = null;
      state.delay = null;
      state.pgNeverReady = false;
      state.bazarrRunFails = "";
    },
  };

  const fail = (message: string) =>
    Object.assign(new Error(message), { code: 1 });
  const option = (args: string[], flag: string) => {
    const index = args.indexOf(flag);
    return index === -1 ? undefined : args[index + 1];
  };
  const labelsOf = (args: string[]) =>
    args.flatMap((arg, index) => (args[index - 1] === "--label" ? [arg] : []));
  const operands = (args: string[]) =>
    args.filter(
      (arg, index) =>
        !arg.startsWith("-") && !args[index - 1]?.startsWith("--"),
    );
  // docker's own rule: label=key matches any value, label=key=value only that.
  const matches = (args: string[]) => {
    const wanted = option(args, "--filter")?.replace(/^label=/, "");
    return (labels: string[]) =>
      wanted === undefined ||
      labels.includes(wanted) ||
      (!wanted.includes("=") && labels.some((l) => l.split("=")[0] === wanted));
  };

  function handle(args: string[]): string {
    const [command, ...rest] = args;
    if (command === "version") return "27.0.0\n";
    if (command === "image") return "sha256:fake\n";
    if (command === "volume") {
      const [sub, ...more] = rest;
      if (sub === "create") {
        const name = more[more.length - 1];
        if (!volumes.has(name)) volumes.set(name, labelsOf(more));
        return `${name}\n`;
      }
      if (sub === "rm") {
        for (const name of operands(more)) volumes.delete(name);
        return "";
      }
      if (sub === "ls") {
        const match = matches(more);
        return [...volumes]
          .filter(([, labels]) => match(labels))
          .map(([name]) => `${name}\n`)
          .join("");
      }
    }
    if (command === "network") {
      const [sub, ...more] = rest;
      if (sub === "create") {
        const name = more[more.length - 1];
        networks.set(name, labelsOf(more));
        return `${name}\n`;
      }
      if (sub === "rm") {
        for (const name of operands(more)) networks.delete(name);
        return "";
      }
      if (sub === "ls") {
        // The suite must list network names, not the table docker prints
        // without -q, whose header and driver columns would be fed to rm.
        if (!more.includes("-q")) {
          throw fail("docker network ls must pass -q");
        }
        const match = matches(more);
        return [...networks]
          .filter(([, labels]) => match(labels))
          .map(([name]) => `${name}\n`)
          .join("");
      }
    }
    if (command === "run") {
      const name = option(rest, "--name") as string;
      const publish = option(rest, "--publish");
      const port = publish ? Number(publish.split(":")[1]) : undefined;
      const volume = option(rest, "--volume");
      const env = args.flatMap((arg, index) =>
        args[index - 1] === "--env" ? [arg] : [],
      );
      const network = option(rest, "--network");
      state.beforeRun?.(port as number);
      if (containers.has(name)) {
        throw fail(`Conflict. The container name "/${name}" is already in use`);
      }
      if (network && !networks.has(network)) {
        throw fail(`network ${network} not found`);
      }
      // A failure of the instance's own run that no other port would fix,
      // such as an image the daemon does not have. The Postgres run is
      // left alone: only the Bazarr one is made to fail.
      if (state.bazarrRunFails && !name.endsWith("-postgres")) {
        throw fail(state.bazarrRunFails);
      }
      const taken =
        port === undefined
          ? false
          : [...containers.values()].some((c) => c.running && c.port === port);
      containers.set(name, {
        name,
        port,
        labels: labelsOf(rest),
        volume: volume?.split(":")[0] ?? name,
        network,
        env,
        running: !taken,
      });
      if (!volume || !volumes.has(volume.split(":")[0])) {
        if (volume) volumes.set(volume.split(":")[0], []);
      }
      if (taken) {
        throw fail(
          `Bind for 127.0.0.1:${port} failed: port is already allocated`,
        );
      }
      return `${name}\n`;
    }
    if (command === "restart") {
      const name = rest[0];
      if (!containers.get(name)?.running) {
        throw fail(`No such container: ${name}`);
      }
      return `${name}\n`;
    }
    if (command === "rm") {
      for (const name of operands(rest)) containers.delete(name);
      return "";
    }
    if (command === "ps") {
      const match = matches(rest);
      return [...containers.values()]
        .filter((c) => match(c.labels))
        .map((c) => `${c.name}\n`)
        .join("");
    }
    if (command === "logs" || command === "exec") {
      const name = rest[0];
      if (!containers.get(name)?.running) {
        throw fail(`No such container: ${name}`);
      }
      if (
        command === "exec" &&
        rest.includes("pg_isready") &&
        state.pgNeverReady
      ) {
        throw fail("pg_isready: no response");
      }
      return command === "logs" ? "BAZARR is started\n" : `key-of-${name}`;
    }
    throw fail(`unexpected: docker ${args.join(" ")}`);
  }

  return { state, handle };
});

vi.mock("node:child_process", () => ({
  execFile: (file: string, args: string[], ...rest: unknown[]) => {
    const done = rest.pop() as (
      error: Error | null,
      result?: { stdout: string; stderr: string },
    ) => void;
    const options = rest[0] as { timeout?: number } | undefined;
    docker.state.calls.push({
      args,
      timeout: options?.timeout,
      seq: docker.state.clock++,
    });
    // Answered on a later turn, as a real process is, so that concurrent
    // callers interleave. A test may stretch a call on the shared clock,
    // which under fake timers is time the test drives itself. A call
    // stretched past its own bound is killed with SIGTERM, as Node's
    // execFile kills a call that outlives its timeout.
    const wait = docker.state.delay?.(args) ?? 1;
    if (options?.timeout !== undefined && wait > options.timeout) {
      setTimeout(
        () =>
          done(
            Object.assign(
              new Error(`Command failed: ${file} ${args.join(" ")}\n`),
              { killed: true, signal: "SIGTERM", code: null },
            ),
          ),
        options.timeout,
      );
      return;
    }
    setTimeout(() => {
      try {
        if (file !== "docker") throw new Error(`unexpected: ${file}`);
        done(null, { stdout: docker.handle(args), stderr: "" });
      } catch (error) {
        done(error as Error);
      }
    }, wait);
  },
}));

interface FetchCall {
  url: string;
  init?: RequestInit;
  /** When the request happened, on the same clock as the stubbed docker's calls. */
  seq: number;
}

const http: {
  calls: FetchCall[];
  /** The status a request is answered with. */
  status: (url: string, init?: RequestInit) => number;
  /** A per-URL answer the stub stands in for, or null for the default. */
  route: (url: string, init?: RequestInit) => Response | null;
  /** How long a request takes, on the same clock as the stubbed docker's. */
  delay: ((url: string, init?: RequestInit) => number) | null;
  /** The body a request is answered with. */
  body: (url: string, init?: RequestInit) => string;
} = {
  calls: [],
  status: () => 200,
  route: () => null,
  delay: null,
  body: () => "",
};

function stubFetch() {
  http.calls = [];
  http.status = (_url, init) => (init?.method === "POST" ? 204 : 200);
  http.delay = null;
  http.body = () => "";
  // The settings API echoes the base the URL is under, as an instance
  // configured with that base does: what the start checks to prove the
  // base took effect.
  http.route = (url, init) => {
    if (init?.method !== "POST" && url.endsWith("/api/system/settings")) {
      const base = new URL(url).pathname.replace(
        /\/api\/system\/settings$/,
        "",
      );
      return Response.json({ general: { base_url: base } });
    }
    // The supervisor answers its status route with JSON, and only ever has
    // that route under a base it booted with, which the start relies on.
    if (init?.method !== "POST" && url.endsWith("/_supervisor/status")) {
      return Response.json({ state: "running" });
    }
    return null;
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input);
      http.calls.push({ url, init, seq: docker.state.clock++ });
      const wait = http.delay?.(url, init);
      if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
      const routed = http.route(url, init);
      if (routed) return routed;
      const status = http.status(url, init);
      return new Response(status === 204 ? null : http.body(url, init), {
        status,
      });
    }),
  );
}

let runDir = "";

async function newRunDir() {
  runDir = await mkdtemp(join(tmpdir(), "bazarr-e2e-"));
  process.env.BAZARR_E2E_RUN_DIR = runDir;
}

beforeEach(async () => {
  // The instance the harness answers to is the environment's, so each test
  // starts from none of it, not from the shell's or the last test's.
  delete process.env.BAZARR_E2E_URL;
  delete process.env.BAZARR_E2E_API_KEY;
  delete process.env.BAZARR_E2E_BASE_PATH;
  docker.state.reset();
  stubFetch();
  // A shell that exports the PostgreSQL variant or an external instance
  // must not leak either into any test: every start in this file would
  // boot an extra container and network, and an exported URL makes the
  // global teardown, which some tests here call, skip its cleanup.
  // Stubbed empty here and restored in the afterEach below, so the tests
  // that select the variant or an instance do it for themselves.
  vi.stubEnv("BAZARR_E2E_POSTGRES", "");
  vi.stubEnv("BAZARR_E2E_URL", "");
  vi.stubEnv("BAZARR_E2E_API_KEY", "");
  await newRunDir();
});

afterEach(async () => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  delete process.env.BAZARR_E2E_URL;
  delete process.env.BAZARR_E2E_API_KEY;
  delete process.env.BAZARR_E2E_BASE_PATH;
  await rm(runDir, { recursive: true, force: true });
  delete process.env.BAZARR_E2E_RUN_DIR;
});

function budget(timeout: number) {
  return {
    timeout,
    setTimeout(value: number) {
      this.timeout = value;
    },
    annotations: [] as { type: string; description?: string }[],
  };
}

/**
 * Stretches every bounded stage of a sub-path start to all but one second
 * of its own bound, on the fake clock: the docker calls and the requests
 * between and after the readiness phases, and the two readiness phases
 * themselves, which answer 503 until their time is up. `phases` is how
 * long each readiness phase takes, within its cap, and `collisions` is
 * how many port races the start loses first, each at the whole bound of
 * its own stages: the two docker calls that made the attempt and the
 * removal of what they left, both calls of the removal sharing its
 * allowance. The settings request under the base doubles as the readiness
 * probe while its phase runs, so it stays inside the probe's own bound,
 * and takes its own share once the phases are done; with both phases
 * near their caps, the whole start takes all but seconds of the startup
 * allowance, which is what the whole-time checks cut into.
 */
function delayedStages(
  start: number,
  phases: { first: number; second: number },
  collisions = 0,
) {
  // Each stage at its own bound less a second: the docker calls that
  // create the container and the key read (DOCKER_MS), the save, the
  // verification requests and the onboarding (REQUEST_MS), the restart
  // (RESTART_MS), and the two calls of a lost attempt's removal sharing
  // the removal allowance (REMOVAL_ALLOWANCE_MS).
  const DOCKER_CALL = DOCKER_MS - 1_000;
  const KEY_READ = DOCKER_MS - 1_000;
  const REQUEST = REQUEST_MS - 1_000;
  const RESTART = RESTART_MS - 1_000;
  const REMOVE_CALL = (REMOVAL_ALLOWANCE_MS - 1_000) / 2;
  const collisionMs = collisions * (2 * DOCKER_CALL + 2 * REMOVE_CALL);
  const firstEnd = collisionMs + 2 * DOCKER_CALL + phases.first;
  const secondEnd = firstEnd + KEY_READ + REQUEST + RESTART + phases.second;
  docker.state.delay = (args) => {
    if (args[0] === "run") return DOCKER_CALL;
    if (args[0] === "rm") return REMOVE_CALL;
    if (args[0] === "volume") {
      return args[1] === "rm" ? REMOVE_CALL : DOCKER_CALL;
    }
    if (args[0] === "exec") return KEY_READ;
    if (args[0] === "restart") return RESTART;
    return 0;
  };
  if (collisions > 0) {
    let runs = 0;
    docker.state.beforeRun = (port) => {
      if (runs >= collisions) return;
      runs += 1;
      // Another run on this machine got to this free port first and
      // holds it, so the start loses the race and tries the next port.
      const theirs = `${E2E_LABEL}-theirs-${runs}`;
      docker.state.containers.set(theirs, {
        name: theirs,
        port,
        labels: [E2E_LABEL],
        volume: theirs,
        running: true,
      });
      docker.state.volumes.set(theirs, []);
    };
  }
  http.delay = (url, init) => {
    // The save and the onboarding are POSTs, never readiness probes, so
    // they can wait for their share; so can the verification requests,
    // which carry the API key the probes never send, once their phase is
    // done. Everything else stays at once: the probes have 10 seconds
    // each, and the settings request under the base is one while its
    // phase runs.
    if (init?.method === "POST") return REQUEST;
    if (Date.now() - start < secondEnd) return 0;
    const pathname = new URL(url).pathname;
    if (
      init?.headers &&
      (pathname.endsWith("/_supervisor/status") ||
        pathname.endsWith("/api/system/settings") ||
        pathname.endsWith("/api/system/status"))
    ) {
      return REQUEST;
    }
    return 0;
  };
  const plain = http.route;
  http.route = (url, init) => {
    if (init?.method === "POST") return plain(url, init);
    const parsed = new URL(url);
    const probe =
      parsed.pathname.endsWith("/setup") ||
      parsed.pathname.endsWith("/api/system/settings");
    if (!probe) return plain(url, init);
    const t = Date.now() - start;
    const firstPhase = !parsed.pathname.startsWith("/bazarr");
    if ((firstPhase && t < firstEnd) || (!firstPhase && t < secondEnd)) {
      return new Response("not ready", { status: 503 });
    }
    return plain(url, init);
  };
}

describe("container names and labels", () => {
  it("a start that loses its port to another run's container leaves that container alone", async () => {
    let theirs = "";
    docker.state.beforeRun = (port) => {
      docker.state.beforeRun = null;
      // Another run on this machine got to the same free port first.
      theirs = `${E2E_LABEL}-${port}`;
      docker.state.containers.set(theirs, {
        name: theirs,
        port,
        labels: [E2E_LABEL],
        volume: theirs,
        running: true,
      });
      docker.state.volumes.set(theirs, []);
    };

    const ours = await startBazarr({ image: "bazarr:test" });

    expect(docker.state.containers.get(theirs)?.running).toBe(true);
    expect(docker.state.volumes.has(theirs)).toBe(true);
    expect(ours.name).not.toBe(theirs);
    expect(docker.state.containers.get(ours.name as string)?.running).toBe(
      true,
    );
  });

  it("two starts on one port in the same run: the loser removes only what it created", async () => {
    const starts = [startBazarr({ port: 6790 }), startBazarr({ port: 6790 })];
    // One of them loses the port and gives up; wait for that.
    await Promise.any(
      starts.map((start) =>
        start.then(
          () => new Promise(() => undefined),
          () => undefined,
        ),
      ),
    );

    const left = [...docker.state.containers.values()];
    expect(left).toHaveLength(1);
    expect(left[0]).toMatchObject({ port: 6790, running: true });
    expect([...docker.state.volumes.keys()]).toEqual([left[0].volume]);

    const [first, second] = await Promise.allSettled(starts);
    const winner = [first, second].find((r) => r.status === "fulfilled");
    expect(winner).toBeDefined();
    expect((winner as PromiseFulfilledResult<BazarrInstance>).value.name).toBe(
      left[0].name,
    );
  });

  it("a Bazarr failure that is no port's business is reported, not retried", async () => {
    docker.state.bazarrRunFails = "Unable to find image bazarr:test locally";

    // The start gives up with the error it got, rather than trying the
    // same failure again on every free port the machine has.
    await expect(startBazarr({ image: "bazarr:test" })).rejects.toThrow(
      /Unable to find image/,
    );

    // Exactly one Bazarr run was made to reach that error.
    expect(
      docker.state.calls.filter(
        (call) => call.args[call.args.length - 1] === "bazarr:test",
      ),
    ).toHaveLength(1);
    // Nothing survives the failure: no container, no volume, no network.
    expect([...docker.state.containers.keys()]).toEqual([]);
    expect([...docker.state.volumes.keys()]).toEqual([]);
    expect([...docker.state.networks.keys()]).toEqual([]);
  });

  it("the same failure with the variant on leaves no network behind", async () => {
    process.env.BAZARR_E2E_POSTGRES = "1";
    docker.state.bazarrRunFails = "Unable to find image bazarr:test locally";

    await expect(startBazarr({ image: "bazarr:test" })).rejects.toThrow(
      /Unable to find image/,
    );

    // Still exactly one Bazarr run: the Postgres beside it is not the
    // failure's business, and its removal is what the assertion below is
    // about.
    expect(
      docker.state.calls.filter(
        (call) => call.args[call.args.length - 1] === "bazarr:test",
      ),
    ).toHaveLength(1);
    // Nothing survives the failure: no container, no volume, and the
    // network the variant created went with them.
    expect([...docker.state.containers.keys()]).toEqual([]);
    expect([...docker.state.volumes.keys()]).toEqual([]);
    expect([...docker.state.networks.keys()]).toEqual([]);
  });

  it("begins each worker's port scan at its own parallel index", async () => {
    // Workers that start at once and scan from the same port race one
    // another for it; each scans from its own place in the range
    // instead, so this one, the second, tries 6792 first, 6790 being the
    // range's first port.
    process.env.TEST_PARALLEL_INDEX = "2";
    let firstPort = 0;
    docker.state.beforeRun = (port) => {
      docker.state.beforeRun = null;
      firstPort = port;
    };
    try {
      await startBazarr({ image: "bazarr:test" });
    } finally {
      delete process.env.TEST_PARALLEL_INDEX;
    }
    expect(firstPort).toBe(6792);
  });

  it("a start whose docker call is killed fails once instead of walking the ports", async () => {
    vi.useFakeTimers();
    try {
      // A busy host can hold a docker call past its bound, and Node kills
      // it with SIGTERM. That is not another worker winning the port, so
      // the start does not answer it by trying the next one, which would
      // pay a fresh volume and container for every port in the range: it
      // removes what it made and fails with the kill's own error.
      docker.state.delay = (args) => (args[0] === "run" ? 900_000 : 0);
      let outcome: { ok: boolean; error?: unknown } | null = null;
      const start = startBazarr({ image: "bazarr:test" });
      start.then(
        () => {
          outcome = { ok: true };
        },
        (error) => {
          outcome = { ok: false, error };
        },
      );
      for (let step = 0; outcome === null && step < 300; step += 1) {
        await vi.advanceTimersByTimeAsync(5_000);
        await new Promise((resolve) => realTimeout(resolve, 0));
      }
      expect(outcome).toMatchObject({
        ok: false,
        error: { killed: true, signal: "SIGTERM" },
      });
      // One attempt only, and what it made is gone: the kill is the
      // start's own failure, not a port somebody else won.
      expect(
        docker.state.calls.filter((call) => call.args[0] === "run"),
      ).toHaveLength(1);
      expect(docker.state.containers.size).toBe(0);
      expect(docker.state.volumes.size).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("every container and its volume carry the run's label and name", async () => {
    const instance = await startBazarr({ image: "bazarr:test" });
    const name = instance.name as string;
    const container = docker.state.containers.get(name) as FakeContainer;
    const runLabel = `${E2E_RUN_LABEL}=${runId()}`;

    expect(name.startsWith(`${E2E_LABEL}-${runId()}-`)).toBe(true);
    expect(container.labels).toEqual(
      expect.arrayContaining([E2E_LABEL, runLabel]),
    );
    expect(docker.state.volumes.get(container.volume)).toEqual(
      expect.arrayContaining([E2E_LABEL, runLabel]),
    );
  });

  it("the run id follows the run directory and is safe in a docker name", async () => {
    const first = runId();
    await newRunDir();
    const second = runId();

    expect(second).not.toBe(first);
    expect(runId()).toBe(second);
    expect(second).toMatch(/^[A-Za-z0-9][A-Za-z0-9_.-]*$/);
  });

  it("global teardown removes what this run left behind and nothing of another run", async () => {
    const mine = `${E2E_LABEL}-${runId()}-6790-dead`;
    const runLabel = `${E2E_RUN_LABEL}=${runId()}`;
    const other = `${E2E_LABEL}-otherrun-6791-beef`;
    const otherLabel = `${E2E_RUN_LABEL}=otherrun`;
    docker.state.containers.set(mine, {
      name: mine,
      port: 6790,
      labels: [E2E_LABEL, runLabel],
      volume: mine,
      running: true,
    });
    docker.state.volumes.set(mine, [E2E_LABEL, runLabel]);
    docker.state.containers.set(other, {
      name: other,
      port: 6791,
      labels: [E2E_LABEL, otherLabel],
      volume: other,
      running: true,
    });
    docker.state.volumes.set(other, [E2E_LABEL, otherLabel]);

    await globalTeardown();

    expect([...docker.state.containers.keys()]).toEqual([other]);
    expect([...docker.state.volumes.keys()]).toEqual([other]);
  });
});

describe("the PostgreSQL variant", () => {
  // The variant is stubbed empty before every test of this file and the
  // environment is restored after it, so a shell that exports the variant
  // reaches no test that does not ask for it, and the ones that do set it
  // for themselves.

  it("is off unless the environment selects it", () => {
    expect(postgresVariant()).toBe(false);
    process.env.BAZARR_E2E_POSTGRES = "1";
    expect(postgresVariant()).toBe(true);
  });

  it("boots a Postgres beside the instance and points the instance at it", async () => {
    process.env.BAZARR_E2E_POSTGRES = "1";
    const runLabel = `${E2E_RUN_LABEL}=${runId()}`;

    const instance = await startBazarr({ image: "bazarr:test" });
    const name = instance.name as string;
    const network = `${name}-net`;
    const postgresName = `${network}-postgres`;
    const postgres = docker.state.containers.get(postgresName);
    const bazarr = docker.state.containers.get(name) as FakeContainer;

    // One network per instance, named for it, labelled for the run.
    expect(docker.state.networks.get(network)).toEqual(
      expect.arrayContaining([E2E_LABEL, runLabel]),
    );
    // The Postgres runs on that network, and so does the instance.
    expect(postgres?.running).toBe(true);
    expect(postgres?.network).toBe(network);
    expect(bazarr.network).toBe(network);
    // The instance is told, through the environment, to use that Postgres.
    expect(bazarr.env).toEqual(
      expect.arrayContaining([
        "POSTGRES_ENABLED=true",
        `POSTGRES_HOST=${postgresName}`,
        "POSTGRES_PORT=5432",
        "POSTGRES_USERNAME=bazarr",
        "POSTGRES_DATABASE=bazarr",
      ]),
    );
    expect(bazarr.env?.join("\n")).toMatch(/POSTGRES_PASSWORD=.+/);
    // The Postgres is mounted on a tmpfs, not the volume the image would
    // declare, so the run leaves no anonymous volume behind.
    expect(docker.state.calls).toContainEqual(
      expect.objectContaining({
        args: expect.arrayContaining([
          "run",
          "--name",
          postgresName,
          "--tmpfs",
          "/var/lib/postgresql/data",
        ]),
      }),
    );
    // Readiness was polled against the Postgres itself, over TCP, through
    // a bounded probe call; the shared clock field says when it happened.
    expect(docker.state.calls).toContainEqual(
      expect.objectContaining({
        args: expect.arrayContaining([
          "exec",
          postgresName,
          "pg_isready",
          "-h",
          "127.0.0.1",
          "-U",
          "bazarr",
          "-d",
          "bazarr",
        ]),
        timeout: expect.any(Number),
      }),
    );
  });

  it("a Postgres that never answers is cleaned up, not retried or leaked", async () => {
    process.env.BAZARR_E2E_POSTGRES = "1";
    docker.state.pgNeverReady = true;

    // The start fails with the readiness error rather than trying every
    // port on the machine for a database that is the same dead on each.
    await expect(
      startBazarr({ image: "bazarr:test", postgresReadyCapMs: 300 }),
    ).rejects.toThrow(/was not ready/);

    // Exactly one Postgres was started to reach that error: one network,
    // one run naming the Postgres container. The retry loop the ports
    // exist for starts another of each per port, which is what this test
    // fails without the no-retry fix.
    expect(
      docker.state.calls.filter(
        (call) => call.args[0] === "network" && call.args[1] === "create",
      ),
    ).toHaveLength(1);
    expect(
      docker.state.calls.filter(
        (call) =>
          call.args[0] === "run" &&
          call.args.some(
            (arg) => typeof arg === "string" && arg.endsWith("-postgres"),
          ),
      ),
    ).toHaveLength(1);

    // Nothing survives the failure: no container, no volume, no network.
    expect([...docker.state.containers.keys()]).toEqual([]);
    expect([...docker.state.volumes.keys()]).toEqual([]);
    expect([...docker.state.networks.keys()]).toEqual([]);
  });

  it("stopping the instance removes its Postgres and network with it", async () => {
    process.env.BAZARR_E2E_POSTGRES = "1";

    const instance = await startBazarr({ image: "bazarr:test" });
    await instance.stop();

    expect([...docker.state.containers.keys()]).toEqual([]);
    expect([...docker.state.networks.keys()]).toEqual([]);
  });

  it("a start without the variant runs no Postgres and no network", async () => {
    const instance = await startBazarr({ image: "bazarr:test" });
    const bazarr = docker.state.containers.get(
      instance.name as string,
    ) as FakeContainer;

    expect([...docker.state.networks.keys()]).toEqual([]);
    expect(bazarr.network).toBeUndefined();
    expect(bazarr.env ?? []).toEqual([]);
  });

  it("global teardown removes the networks this run left behind", async () => {
    process.env.BAZARR_E2E_POSTGRES = "1";
    const instance = await startBazarr({ image: "bazarr:test" });
    // The worker that owned the instance died without stopping it.
    docker.state.containers.delete(instance.name as string);

    await globalTeardown();

    expect([...docker.state.networks.keys()]).toEqual([]);
  });
});

describe("external instances", () => {
  it("accepts an instance served under a sub-path, keeping its path", async () => {
    process.env.BAZARR_E2E_API_KEY = "key";
    for (const url of [
      "https://bazarr.example.lan/bazarr",
      "https://bazarr.example.lan/bazarr/",
    ]) {
      const instance = await externalBazarr(url);
      expect(instance.baseURL).toBe("https://bazarr.example.lan/bazarr");
    }
  });

  it("accepts the root of an instance, with or without a slash", async () => {
    process.env.BAZARR_E2E_API_KEY = "key";
    for (const url of [
      "https://bazarr.example.lan",
      "https://bazarr.example.lan/",
    ]) {
      const instance = await externalBazarr(url);
      expect(instance.baseURL).toBe("https://bazarr.example.lan");
    }
  });

  it("refuses a URL that is not a URL, before any request is made", async () => {
    await expect(externalBazarr("bazarr.example.lan")).rejects.toThrow();
    expect(http.calls).toHaveLength(0);
  });

  it("refuses an instance that asks for a login but hands over no key", async () => {
    // The shell Bazarr serves a session it does not let in: window.Bazarr
    // carries no API key, and the app opens on its login form.
    const shell =
      "<script>window.Bazarr = JSON.parse(" +
      '`{"baseUrl": "/", "canUpdate": false, "hasUpdate": false, "isConfigured": true}`' +
      ");</script>";
    http.body = () => shell;

    await expect(externalBazarr("https://bazarr.example.lan")).rejects.toThrow(
      /BAZARR_E2E_API_KEY[\s\S]*401/,
    );
    expect(http.calls.map((call) => call.url)).toEqual([
      "https://bazarr.example.lan/",
    ]);
  });
});

describe("the sub-path variant", () => {
  it("starts its container and then serves it under the configured base path", async () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    const instance = await startBazarr({ image: "bazarr:test" });

    expect(instance.baseURL).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/bazarr$/);

    // general.base_url is saved through the settings API, at the root the
    // fresh install still answers on, and applied by a restart.
    const settings = http.calls.find(
      (call) =>
        call.init?.method === "POST" &&
        call.url.endsWith("/api/system/settings"),
    );
    expect(settings).toBeDefined();
    expect(
      (settings?.init?.body as FormData).get("settings-general-base_url"),
    ).toBe("/bazarr");

    // The container is restarted whole, after the save, and readiness is
    // polled under the base path only after that, so the instance handed out
    // answers there on its own.
    const restart = docker.state.calls.find(
      (call) => call.args[0] === "restart" && call.args[1] === instance.name,
    );
    expect(restart).toBeDefined();
    expect(restart?.timeout).toBeGreaterThan(0);
    const save = http.calls.find(
      (call) =>
        call.init?.method === "POST" &&
        call.url.endsWith("/api/system/settings"),
    );
    expect((save?.seq ?? Infinity) < (restart?.seq ?? -1)).toBe(true);
    const underBase = http.calls.filter((call) => call.url.includes("/bazarr"));
    expect(underBase.length).toBeGreaterThan(0);
    for (const call of underBase) {
      expect(call.seq).toBeGreaterThan(restart?.seq ?? -1);
    }

    // The start proves the base took effect: the settings under the base
    // report it, and the root no longer answers the API.
    expect(
      http.calls.some(
        (call) =>
          call.init?.method !== "POST" &&
          call.url === `${instance.baseURL}/api/system/settings`,
      ),
    ).toBe(true);
    expect(
      http.calls.some(
        (call) =>
          call.url.endsWith("/api/system/status") &&
          !call.url.includes("/bazarr"),
      ),
    ).toBe(true);
  });

  it("fails and removes the container when the base path does not take effect", async () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    // A stale process keeps answering at the root: the base's settings say
    // the base, but the root still speaks JSON.
    const plain = http.route;
    http.route = (url, init) => {
      if (
        init?.method !== "POST" &&
        new URL(url).pathname === "/api/system/status"
      ) {
        return Response.json({ stale: true });
      }
      return plain(url, init);
    };

    await expect(startBazarr({ image: "bazarr:test" })).rejects.toThrow(
      /did not take effect/,
    );

    expect(docker.state.containers.size).toBe(0);
    expect(docker.state.volumes.size).toBe(0);
  });

  it("fails and removes the container when a save is accepted but ignored, answering the app's front page", async () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    // The save was accepted and ignored: under the base the instance does
    // not answer the settings API but its own front page, HTML, the way a
    // real install does in this case.
    http.route = (url, init) => {
      if (
        init?.method !== "POST" &&
        url.endsWith("/bazarr/api/system/settings")
      ) {
        return new Response("<html>the app's front page</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        });
      }
      return null;
    };

    await expect(startBazarr({ image: "bazarr:test" })).rejects.toThrow(
      /did not take effect/,
    );

    expect(docker.state.containers.size).toBe(0);
    expect(docker.state.volumes.size).toBe(0);
  });

  it("fails and removes the container when the settings under the base report another base", async () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    // The save stored something else: the settings under the base report
    // it, and the start hands out no instance whose base is a lie.
    http.route = (url, init) => {
      if (
        init?.method !== "POST" &&
        url.endsWith("/bazarr/api/system/settings")
      ) {
        return Response.json({ general: { base_url: "/other" } });
      }
      return null;
    };

    await expect(startBazarr({ image: "bazarr:test" })).rejects.toThrow(
      /general\.base_url/,
    );

    expect(docker.state.containers.size).toBe(0);
    expect(docker.state.volumes.size).toBe(0);
  });

  it("fails and removes the container when the supervisor under the base answers the app's front page", async () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    // The supervisor in front of the app booted without the base, so it
    // never registered the base's status route and its catch-all serves the
    // SPA instead: the in-app-restart failure the whole-container restart
    // exists to avoid.
    const plain = http.route;
    http.route = (url, init) => {
      if (
        init?.method !== "POST" &&
        url.endsWith("/bazarr/_supervisor/status")
      ) {
        return new Response("<html>the app's front page</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        });
      }
      return plain(url, init);
    };

    await expect(startBazarr({ image: "bazarr:test" })).rejects.toThrow(
      /did not take effect/,
    );

    expect(docker.state.containers.size).toBe(0);
    expect(docker.state.volumes.size).toBe(0);
  });

  it("leaves a run with no base path at the root of its host", async () => {
    const instance = await startBazarr({ image: "bazarr:test" });
    expect(instance.baseURL).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    expect(
      http.calls.some(
        (call) =>
          call.init?.method !== "POST" &&
          call.url.endsWith("/api/system/status"),
      ),
    ).toBe(false);
  });

  it("gives a start under a base path the time of both readiness phases", async () => {
    vi.useFakeTimers();
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    try {
      // Boot and the base-path restart each take 150 of the 180 seconds
      // their phase is given: 300 seconds in all, which the one-phase
      // allowance never covered. The clock is the fake one, so the test
      // itself takes milliseconds.
      const start = Date.now();
      const plain = http.route;
      http.route = (url, init) => {
        if (init?.method === "POST") return plain(url, init);
        if (Date.now() - start < 150_000) {
          return new Response("not ready", { status: 503 });
        }
        // The second phase is the restart's, not the boot's.
        if (Date.now() - start < 300_000 && url.includes("/bazarr")) {
          return new Response("not ready", { status: 503 });
        }
        return plain(url, init);
      };

      const slot: ContainerSlot = { held: null };
      const budgeted = budget(60_000);
      let outcome: { ok: boolean; error?: unknown } | null = null;
      const started = containerForFile(slot, "a.spec.ts", budgeted);
      started.then(
        () => {
          outcome = { ok: true };
        },
        (error) => {
          outcome = { ok: false, error };
        },
      );
      // Keep the fake clock moving until the start finishes; it never
      // advances on its own. Two phases of 150 seconds need well under
      // this many 5-second steps, and the caps end the loop anyway.
      for (let step = 0; outcome === null && step < 200; step += 1) {
        await vi.advanceTimersByTimeAsync(5_000);
      }
      expect(outcome).toEqual({ ok: true });
      // The delayed start fits the test's budget: the allowance covers the
      // second readiness phase, which a one-phase budget would not.
      const elapsed = Date.now() - start;
      expect(elapsed).toBeGreaterThan(250_000);
      expect(elapsed).toBeLessThanOrEqual(budgeted.timeout - 60_000);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("bounded probes", () => {
  it("readiness gives up at its cap when the server accepts and never answers", async () => {
    vi.unstubAllGlobals();
    const sockets = new Set<Socket>();
    const server = createServer((socket) => {
      sockets.add(socket);
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", resolve),
    );
    const { port } = server.address() as AddressInfo;
    try {
      const started = Date.now();
      await expect(
        waitUntilReady(`http://127.0.0.1:${port}`, null, {
          capMs: 400,
          pollMs: 50,
          probeMs: 150,
        }),
      ).rejects.toThrow(/not ready/);
      // Unbounded, the first request would wait for fetch's own five-minute
      // header timeout. The margin is for a busy machine, not for the probe.
      expect(Date.now() - started).toBeLessThan(10_000);
    } finally {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    }
  }, 30_000);

  it("every docker probe and every request carries a bound", async () => {
    await sharedBazarr();
    process.env.BAZARR_E2E_URL = "https://bazarr.example.lan";
    await externalBazarr("https://bazarr.example.lan");

    const probes = docker.state.calls.filter((call) =>
      ["logs", "exec"].includes(call.args[0]),
    );
    expect(probes.length).toBeGreaterThan(0);
    for (const call of probes) {
      expect(call.timeout, call.args[0]).toBeGreaterThan(0);
    }
    expect(http.calls.length).toBeGreaterThan(0);
    for (const call of http.calls) {
      expect(call.init?.signal, call.url).toBeInstanceOf(AbortSignal);
    }
  });
});

describe("the startup allowance", () => {
  it("is at least its readiness phases' caps plus every other stage's bound", () => {
    // The slack is the sum of the other stages' bounds, so a start in
    // which every stage takes its whole time fits the one test's budget;
    // cutting it below that sum fails here instead of passing such a
    // start.
    expect(startupAllowanceMs()).toBeGreaterThanOrEqual(
      READY_CAP_MS + STARTUP_STAGES_MS,
    );
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    try {
      expect(startupAllowanceMs()).toBeGreaterThanOrEqual(
        2 * READY_CAP_MS + STARTUP_STAGES_MS,
      );
    } finally {
      delete process.env.BAZARR_E2E_BASE_PATH;
    }
  });

  it("counts every one of the stages' bounds in the sum", () => {
    // The stage sum is checked against this test's own count of the
    // bounds it is made of, not against the code that adds them, so a
    // term dropped from the sum, the restart's or a lost race's say,
    // fails here instead of quietly shrinking the allowance and every
    // budget built on it.
    expect(STARTUP_STAGES_MS).toBe(
      3 * DOCKER_MS + // the docker calls that create the container and the key read
        REQUEST_MS + // the save of the base
        RESTART_MS + // the restart that applies it
        4 * REQUEST_MS + // the verification requests and the onboarding
        PORT_RETRIES * (2 * DOCKER_MS + REMOVAL_ALLOWANCE_MS), // the lost races for a port
    );
  });
});

describe("the shared instance", () => {
  it("is removed when onboarding fails, so nothing is left for teardown to miss", async () => {
    http.status = (_url, init) => (init?.method === "POST" ? 500 : 200);

    await expect(sharedBazarr()).rejects.toThrow(/onboarding answered 500/);

    expect(docker.state.containers.size).toBe(0);
    expect(docker.state.volumes.size).toBe(0);
    expect(await readSharedRecord()).toBeNull();
    expect(await readdir(runDir)).toEqual([]);
  });

  it("gives the startup allowance to the test that starts it", async () => {
    const first = budget(60_000);
    await sharedBazarr(first);
    expect(first.timeout).toBeGreaterThan(60_000);
    expect(first.timeout - 60_000).toBe(startupAllowanceMs());
  });

  it("gives no allowance to a test that finds it running", async () => {
    await sharedBazarr();
    const later = budget(60_000);
    const instance = await sharedBazarr(later);
    expect(instance.apiKey).toBe(`key-of-${instance.name}`);
    expect(later.timeout).toBe(60_000);
  });

  it("leaves a test with no timeout without one when it starts it", async () => {
    // --debug and --timeout 0 run every test with a timeout of 0, none at all.
    const debugging = budget(0);
    await sharedBazarr(debugging);
    expect(debugging.timeout).toBe(0);
  });

  it("covers a start in which every bounded stage takes its whole time", async () => {
    vi.useFakeTimers();
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    try {
      // Every bounded stage takes all but one second of its own bound,
      // both readiness phases answer 503 until one poll before their
      // cap, and the start loses PORT_RETRIES races for its port first,
      // each race paying the whole bound of its own stages: the two
      // docker calls that made the attempt and the removal of what they
      // left. The whole start then finishes well inside the startup
      // allowance, so a stage dropped from the sum, or slack cut off
      // it, no longer fits the one test's budget and fails here
      // instead of passing such a start.
      const start = Date.now();
      delayedStages(
        start,
        {
          first: READY_CAP_MS - POLL_MS,
          second: READY_CAP_MS - POLL_MS,
        },
        PORT_RETRIES,
      );
      const budgeted = budget(60_000);
      let outcome: { ok: boolean; error?: unknown } | null = null;
      const started = sharedBazarr(budgeted);
      started.then(
        () => {
          outcome = { ok: true };
        },
        (error) => {
          outcome = { ok: false, error };
        },
      );
      // The clock is driven in small steps: the real I/O of the start
      // (the port scan, the lock, the record) is answered in real turns
      // the fake clock never moves in, and a big blind step would charge
      // it fake time that has nothing to do with the stages.
      for (let step = 0; outcome === null && step < 2_500; step += 1) {
        await vi.advanceTimersByTimeAsync(1_000);
        await new Promise((resolve) => realTimeout(resolve, 0));
      }
      expect(outcome).toEqual({ ok: true });
      const elapsed = Date.now() - start;
      const allowance = startupAllowanceMs();
      expect(elapsed).toBeLessThanOrEqual(allowance);
      expect(elapsed).toBeGreaterThan(allowance - 60_000);
    } finally {
      vi.useRealTimers();
    }
  });

  it("lets a waiter for the shared instance outlast such a start", async () => {
    vi.useFakeTimers();
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    try {
      // Two workers ask for the shared instance at once. The one that
      // loses the lock waits for the other's whole start: both readiness
      // phases at one poll inside their cap, every bounded stage at
      // all but a second of its own bound, and PORT_RETRIES lost port
      // races first, each at the whole bound of its own stages, so the
      // start ends close enough to the lock's deadline that a deadline
      // short of a whole retry-inclusive start would time the waiter out
      // here.
      const start = Date.now();
      delayedStages(
        start,
        {
          first: READY_CAP_MS - POLL_MS,
          second: READY_CAP_MS - POLL_MS,
        },
        PORT_RETRIES,
      );
      const instances: Array<BazarrInstance | null> = [null, null];
      const outcomes: Array<{ ok: boolean; error?: unknown } | null> = [
        null,
        null,
      ];
      const callers = [0, 1].map((index) =>
        sharedBazarr(budget(60_000)).then(
          (instance) => {
            instances[index] = instance;
            outcomes[index] = { ok: true };
          },
          (error) => {
            outcomes[index] = { ok: false, error };
          },
        ),
      );
      for (
        let step = 0;
        outcomes.some((outcome) => outcome === null) && step < 400;
        step += 1
      ) {
        await vi.advanceTimersByTimeAsync(5_000);
        await new Promise((resolve) => realTimeout(resolve, 0));
      }
      await Promise.allSettled(callers);
      // The waiter got the starter's record, not the timeout message.
      expect(outcomes[0]).toEqual({ ok: true });
      expect(outcomes[1]).toEqual({ ok: true });
      expect(instances[1]?.name).toBe(instances[0]?.name);
      expect(instances[1]?.apiKey).toBe(instances[0]?.apiKey);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the container a stateful spec file gets", () => {
  it("is kept for the tests of one file and replaced for the next", async () => {
    const slot: ContainerSlot = { held: null };

    const first = await containerForFile(slot, "a.spec.ts", budget(60_000));
    const again = await containerForFile(slot, "a.spec.ts", budget(60_000));
    const next = await containerForFile(slot, "b.spec.ts", budget(60_000));

    expect(again).toBe(first);
    expect([...docker.state.containers.keys()]).toEqual([next.name]);
    expect([...docker.state.volumes.keys()]).toEqual([next.name]);
  });

  it("gives the test that replaces it the time to remove the old one, before removing it", async () => {
    const slot: ContainerSlot = { held: null };
    const starting = budget(60_000);
    await containerForFile(slot, "a.spec.ts", starting);
    expect(starting.timeout - 60_000).toBe(startupAllowanceMs());

    const replacing = budget(60_000);
    const old = slot.held?.instance as BazarrInstance;
    let whileRemoving = 0;
    slot.held = {
      file: "a.spec.ts",
      instance: {
        ...old,
        stop: async () => {
          whileRemoving = replacing.timeout;
          await old.stop();
        },
      },
    };
    await containerForFile(slot, "b.spec.ts", replacing);

    expect(whileRemoving - 60_000).toBe(REMOVAL_ALLOWANCE_MS);
    expect(replacing.timeout - 60_000).toBe(
      REMOVAL_ALLOWANCE_MS + startupAllowanceMs(),
    );
  });

  it("leaves a test with no timeout without one when it replaces it", async () => {
    const slot: ContainerSlot = { held: null };
    const debugging = budget(0);
    await containerForFile(slot, "a.spec.ts", debugging);
    await containerForFile(slot, "b.spec.ts", debugging);
    expect(debugging.timeout).toBe(0);
  });
});

interface Row {
  provider_id: string;
  state: string;
  pending_restart?: boolean;
  last_error?: string | null;
}

const CATALOG = ["alpha", "beta"].map((id) => ({
  provider_id: id,
  name: id,
  manifest: { provider_id: id, name: id, description: `${id} subtitles` },
}));

/**
 * The Provider Hub and settings endpoints the provider helper uses, with an
 * install that ends the way the test says: staged for the next restart, or
 * failed with a reason, as the backend records them.
 */
function fakeHub(options: {
  rows: Row[];
  enabled: string[];
  outcome?: Record<string, "staged" | "failed">;
}) {
  const hub = {
    rows: new Map(options.rows.map((row) => [row.provider_id, { ...row }])),
    enabled: [...options.enabled],
    installs: [] as string[],
    enables: [] as string[][],
    restarts: 0,
    jobs: [] as { job_id: number; status: string }[],
  };
  const reply = (status: number, body?: unknown) => ({
    ok: () => status >= 200 && status < 300,
    status: () => status,
    json: async () => body,
  });
  const api = {
    async get(path: string) {
      if (path === "/api/provider-hub/providers") {
        return reply(200, { data: [...hub.rows.values()] });
      }
      if (path === "/api/provider-hub/catalog") {
        return reply(200, { entries: CATALOG });
      }
      if (path === "/api/system/jobs") return reply(200, { data: hub.jobs });
      if (path === "/api/system/settings") {
        return reply(200, { general: { enabled_providers: hub.enabled } });
      }
      return reply(404);
    },
    async post(
      path: string,
      body: {
        data?: { manifest: { provider_id: string } };
        multipart?: FormData;
      },
    ) {
      if (path === "/api/provider-hub/installations") {
        const id = body.data?.manifest.provider_id as string;
        hub.installs.push(id);
        const staged = (options.outcome?.[id] ?? "staged") === "staged";
        hub.rows.set(id, {
          provider_id: id,
          state: staged ? "staged" : "failed",
          pending_restart: staged,
          last_error: staged ? null : `${id}: the bundle did not verify`,
        });
        const jobId = hub.jobs.length + 1;
        hub.jobs.push({
          job_id: jobId,
          status: staged ? "completed" : "failed",
        });
        return reply(202, { job_id: jobId });
      }
      if (path === "/api/system/settings") {
        hub.enabled = (body.multipart as FormData)
          .getAll("settings-general-enabled_providers")
          .map(String);
        hub.enables.push(hub.enabled);
        return reply(204);
      }
      if (path === "/api/system") {
        hub.restarts += 1;
        for (const row of hub.rows.values()) {
          if (row.state === "staged" && row.pending_restart) {
            row.state = "active";
            row.pending_restart = false;
          }
        }
        return reply(204);
      }
      return reply(404);
    },
    async patch() {
      return reply(404);
    },
  };
  return { hub, api: api as unknown as APIRequestContext };
}

const EXTERNAL: BazarrInstance = {
  baseURL: "http://127.0.0.1:1",
  apiKey: "key",
  name: null,
  stop: async () => undefined,
};

describe("recommended providers", () => {
  it("installs a failed provider again, once a run, and never enables it while it stays failed", async () => {
    const { hub, api } = fakeHub({
      rows: [
        { provider_id: "alpha", state: "active" },
        { provider_id: "beta", state: "failed", last_error: "timed out" },
      ],
      enabled: ["alpha"],
      outcome: { beta: "failed" },
    });
    const info = budget(60_000);

    const ready = await ensureRecommendedProviders(
      api,
      EXTERNAL,
      info as unknown as TestInfo,
    );
    await ensureRecommendedProviders(
      api,
      EXTERNAL,
      budget(60_000) as unknown as TestInfo,
    );

    expect(hub.installs).toEqual(["beta"]);
    expect(hub.enabled).toEqual(["alpha"]);
    expect(hub.enables.flat()).not.toContain("beta");
    expect(ready).toEqual(["alpha"]);
    expect(info.annotations).toContainEqual(
      expect.objectContaining({
        description: expect.stringContaining("the bundle did not verify"),
      }),
    );
  });

  it("enables and loads a failed provider whose second install stages", async () => {
    const { hub, api } = fakeHub({
      rows: [
        { provider_id: "alpha", state: "active" },
        { provider_id: "beta", state: "failed", last_error: "timed out" },
      ],
      enabled: ["alpha"],
    });

    const ready = await ensureRecommendedProviders(
      api,
      EXTERNAL,
      budget(60_000) as unknown as TestInfo,
    );

    expect(hub.installs).toEqual(["beta"]);
    expect(hub.enabled).toEqual(["alpha", "beta"]);
    expect(hub.restarts).toBe(1);
    expect(ready).toEqual(["alpha", "beta"]);
  }, 15_000);

  it("keeps the install allowance when one provider is active and another still needs installing", async () => {
    const { hub, api } = fakeHub({
      rows: [{ provider_id: "alpha", state: "active" }],
      enabled: ["alpha"],
    });
    const info = budget(60_000);

    await ensureRecommendedProviders(
      api,
      EXTERNAL,
      info as unknown as TestInfo,
    );

    expect(hub.installs).toEqual(["beta"]);
    expect(info.timeout).toBeGreaterThan(60_000);
    expect(info.timeout - 60_000).toBe(INSTALL_ALLOWANCE_MS);
  }, 15_000);

  it("gives the allowance back when everything is already in place", async () => {
    const { hub, api } = fakeHub({
      rows: [
        { provider_id: "alpha", state: "active" },
        { provider_id: "beta", state: "active" },
      ],
      enabled: ["alpha", "beta"],
    });
    const info = budget(60_000);

    await ensureRecommendedProviders(
      api,
      EXTERNAL,
      info as unknown as TestInfo,
    );

    expect(hub.installs).toEqual([]);
    expect(hub.restarts).toBe(0);
    expect(info.timeout).toBeGreaterThanOrEqual(60_000);
    expect(info.timeout).toBeLessThan(60_000 + 5_000);
  });

  it("leaves a test with no timeout without one, whether it installs or not", async () => {
    const { hub, api } = fakeHub({
      rows: [{ provider_id: "alpha", state: "active" }],
      enabled: ["alpha"],
    });
    const installing = budget(0);
    const inPlace = budget(0);

    await ensureRecommendedProviders(
      api,
      EXTERNAL,
      installing as unknown as TestInfo,
    );
    await ensureRecommendedProviders(
      api,
      EXTERNAL,
      inPlace as unknown as TestInfo,
    );

    expect(hub.installs).toEqual(["beta"]);
    expect(installing.timeout).toBe(0);
    expect(inPlace.timeout).toBe(0);
  }, 15_000);
});
