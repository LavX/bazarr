/* eslint-disable camelcase -- transport names. */
import { describe, expect, it } from "vitest";
import "@/tests";
import type { PluginInstallation, PluginJob } from "@/apis/raw/plugins";
import { installation } from "./fixtures";
import { installedRows } from "./model";
import { attentionCause, openJobs, pluginStatus, runtimeLine } from "./status";

const rowFor = (item: PluginInstallation, others: PluginInstallation[] = []) =>
  installedRows(undefined, [item, ...others], "all")[0];

const job = (action: string, state = "running"): PluginJob => ({
  id: action,
  action,
  state,
  target_kind: "provider",
  target_id: "demo",
});

const base = installation("demo", "translator");

describe("the status badge precedence", () => {
  const table: Array<{
    name: string;
    item: PluginInstallation;
    jobs?: PluginJob[];
    update?: boolean;
    expected: string;
  }> = [
    { name: "active", item: base, expected: "active" },
    {
      name: "off",
      item: { ...base, enabled: false },
      expected: "off",
    },
    {
      name: "update available",
      item: base,
      update: true,
      expected: "update",
    },
    {
      name: "a staged release is activating",
      item: { ...base, state: "staged" },
      expected: "activating",
    },
    {
      name: "an activation job is activating",
      item: base,
      jobs: [job("activate")],
      expected: "activating",
    },
    {
      name: "the staging of a new release is installing",
      item: base,
      jobs: [job("stage_update")],
      expected: "installing",
    },
    {
      name: "an install job is installing",
      item: base,
      jobs: [job("install")],
      expected: "installing",
    },
    {
      name: "a repair job is rebuilding",
      item: { ...base, needs_repair: true },
      jobs: [job("repair")],
      expected: "rebuilding",
    },
    {
      name: "a plugin that needs repair needs attention",
      item: { ...base, needs_repair: true },
      expected: "attention",
    },
    {
      name: "a failed update needs attention before an update is offered",
      item: { ...base, state: "failed", last_error: "It broke." },
      update: true,
      expected: "attention",
    },
    {
      name: "a worker that could not start needs attention",
      item: {
        ...base,
        runtime: {
          phase: "broken",
          in_flight: 0,
          retry_after: null,
          last_error: "It did not start.",
          starts: 1,
          crashes: 1,
          memory_bytes: 0,
        },
      },
      expected: "attention",
    },
    {
      name: "a job that is finished says nothing",
      item: base,
      jobs: [job("repair", "completed")],
      expected: "active",
    },
  ];
  for (const entry of table) {
    it(`says ${entry.expected} for ${entry.name}`, () => {
      const row = {
        ...rowFor(entry.item),
        update: entry.update ? "9.9.9" : null,
      };
      expect(pluginStatus(row, openJobs(entry.jobs))?.key).toBe(entry.expected);
    });
  }

  it("says nothing for a plugin that is not installed", () => {
    const row = rowFor({ ...base, active_version: null, staged_version: null });
    expect(pluginStatus(row, openJobs([]))).toBeNull();
  });

  it("gives the cause of what needs attention in the server's words", () => {
    expect(
      attentionCause({ ...base, state: "failed", last_error: "Why." }),
    ).toBe("Why.");
    expect(attentionCause(base)).toBeNull();
  });

  it("says a revoked release needs attention, switched on or off", () => {
    const revoked = {
      ...base,
      revoked: true,
      last_error:
        "Version 1.0.0 of Example was revoked by the official catalog.",
    };
    expect(attentionCause(revoked)).toBe(revoked.last_error);
    expect(attentionCause({ ...revoked, last_error: null })).toMatch(
      /revoked this version/,
    );
    expect(pluginStatus(rowFor(revoked), openJobs([]))?.key).toBe("attention");
  });
});

describe("the runtime line", () => {
  const runtime = (phase: string, extra = {}) =>
    ({
      phase,
      in_flight: 0,
      retry_after: null,
      last_error: null,
      starts: 0,
      crashes: 0,
      memory_bytes: 0,
      ...extra,
    }) as never;

  it("words every phase, and nothing when the plugin has no worker", () => {
    expect(runtimeLine(runtime("starting"))).toBe("Starting");
    expect(runtimeLine(runtime("healthy", { in_flight: 1 }))).toBe(
      "Healthy, running a request",
    );
    expect(runtimeLine(runtime("healthy", { in_flight: 3 }))).toBe(
      "Healthy, 3 requests running",
    );
    expect(runtimeLine(runtime("idle"))).toBe("Idle, ready for work");
    expect(runtimeLine(runtime("backing_off", { retry_after: 11.2 }))).toBe(
      "Backing off after a failure, trying again in 12 s",
    );
    expect(runtimeLine(runtime("stopped"))).toBe("Stopped, starts when needed");
    expect(runtimeLine(undefined)).toBeNull();
  });
});

describe("who needs a plugin", () => {
  it("shows exactly the dependents the server lists, by name, and works nothing out itself", () => {
    const needed = installation(
      "engine",
      "translator",
      { provides: ["engine.v1"] },
      {
        name: "Engine",
        dependents: ["backend_off", "backend_on"],
        enabled_dependents: ["backend_on"],
      },
    );
    // These two would be found by reading manifests. The page ignores that and takes the server's lists.
    const on = installation(
      "backend_on",
      "translator_backend",
      { extends: { id: "engine", capability: "engine.v1" } },
      { name: "Backend on" },
    );
    const off = installation(
      "backend_off",
      "translator_backend",
      { extends: { id: "engine", capability: "engine.v1" } },
      { name: "Backend off", enabled: false },
    );
    const row = rowFor(needed, [on, off]);
    expect(row.dependents).toEqual(["Backend off", "Backend on"]);
    expect(row.enabledDependents).toEqual(["Backend on"]);
  });

  it("lists nobody when the server lists nobody, even if a manifest says it extends the plugin", () => {
    const needed = installation(
      "engine",
      "translator",
      {},
      { name: "Engine", dependents: [] },
    );
    const extender = installation("extender", "translator_backend", {
      extends: { id: "engine", capability: "engine.v1" },
    });
    expect(rowFor(needed, [extender]).dependents).toEqual([]);
  });
});

describe("the jobs that lock a card", () => {
  const now = Date.parse("2026-11-18T12:00:00Z");
  const at = (updated_at: string | undefined, id = "a"): PluginJob => ({
    ...job("repair"),
    id,
    updated_at,
    created_at: updated_at,
  });

  it("counts a running job that reported recently, and one with no time at all", () => {
    expect(openJobs([at("2026-11-18T11:30:00Z")], now).get("demo")).toEqual(
      new Set(["repair"]),
    );
    expect(openJobs([at(undefined)], now).get("demo")).toEqual(
      new Set(["repair"]),
    );
  });

  it("does not let a job nothing has reported on for hours lock the plugin", () => {
    expect(openJobs([at("2026-11-18T08:00:00Z")], now).get("demo")).toBe(
      undefined,
    );
    expect(
      pluginStatus(
        { ...rowFor(base), update: null },
        openJobs([at("2026-11-18T08:00:00Z")], now),
      )?.key,
    ).toBe("active");
  });
});
