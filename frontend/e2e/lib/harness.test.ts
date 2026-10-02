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
  E2E_LABEL,
  E2E_RUN_LABEL,
  externalBazarr,
  REMOVAL_ALLOWANCE_MS,
  runId,
  startBazarr,
  STARTUP_ALLOWANCE_MS,
  waitUntilReady,
} from "./container";
import { ensureRecommendedProviders, INSTALL_ALLOWANCE_MS } from "./providers";
import { readSharedRecord, sharedBazarr } from "./shared";

interface FakeContainer {
  name: string;
  port: number;
  labels: string[];
  volume: string;
  running: boolean;
}

interface DockerCall {
  args: string[];
  timeout?: number;
}

/**
 * Just enough of docker for the harness: names and host ports are unique the
 * way the daemon keeps them, and a run that loses a port leaves a created,
 * stopped container behind, as `docker run` does.
 */
const docker = vi.hoisted(() => {
  const containers = new Map<string, FakeContainer>();
  const volumes = new Map<string, string[]>();
  const calls: DockerCall[] = [];
  const state = {
    containers,
    volumes,
    calls,
    /** Runs at the start of each `docker run`, to let another run win. */
    beforeRun: null as ((port: number) => void) | null,
    reset() {
      containers.clear();
      volumes.clear();
      calls.length = 0;
      state.beforeRun = null;
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
    if (command === "run") {
      const name = option(rest, "--name") as string;
      const port = Number((option(rest, "--publish") as string).split(":")[1]);
      const volume = (option(rest, "--volume") as string).split(":")[0];
      state.beforeRun?.(port);
      if (containers.has(name)) {
        throw fail(`Conflict. The container name "/${name}" is already in use`);
      }
      if (!volumes.has(volume)) volumes.set(volume, []);
      const taken = [...containers.values()].some(
        (c) => c.running && c.port === port,
      );
      containers.set(name, {
        name,
        port,
        labels: labelsOf(rest),
        volume,
        running: !taken,
      });
      if (taken) {
        throw fail(
          `Bind for 127.0.0.1:${port} failed: port is already allocated`,
        );
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
    docker.state.calls.push({ args, timeout: options?.timeout });
    // Answered on a later turn, as a real process is, so that concurrent
    // callers interleave.
    setTimeout(() => {
      try {
        if (file !== "docker") throw new Error(`unexpected: ${file}`);
        done(null, { stdout: docker.handle(args), stderr: "" });
      } catch (error) {
        done(error as Error);
      }
    }, 1);
  },
}));

interface FetchCall {
  url: string;
  init?: RequestInit;
}

const http: {
  calls: FetchCall[];
  /** The status a request is answered with. */
  status: (url: string, init?: RequestInit) => number;
} = {
  calls: [],
  status: () => 200,
};

function stubFetch() {
  http.calls = [];
  http.status = (_url, init) => (init?.method === "POST" ? 204 : 200);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input);
      http.calls.push({ url, init });
      const status = http.status(url, init);
      return new Response(status === 204 ? null : "", { status });
    }),
  );
}

let runDir = "";

async function newRunDir() {
  runDir = await mkdtemp(join(tmpdir(), "bazarr-e2e-"));
  process.env.BAZARR_E2E_RUN_DIR = runDir;
}

beforeEach(async () => {
  docker.state.reset();
  stubFetch();
  await newRunDir();
});

afterEach(async () => {
  vi.unstubAllGlobals();
  delete process.env.BAZARR_E2E_URL;
  delete process.env.BAZARR_E2E_API_KEY;
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

describe("external instances", () => {
  it("refuses a URL with a path, which the specs cannot address", async () => {
    await expect(
      externalBazarr("https://bazarr.example.lan/bazarr"),
    ).rejects.toThrow(/sub-path|path/i);
    expect(http.calls).toHaveLength(0);
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
    expect(first.timeout - 60_000).toBe(STARTUP_ALLOWANCE_MS);
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
    expect(starting.timeout - 60_000).toBe(STARTUP_ALLOWANCE_MS);

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
      REMOVAL_ALLOWANCE_MS + STARTUP_ALLOWANCE_MS,
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
