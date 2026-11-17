/* eslint-disable camelcase -- transport names. */
import { describe, expect, it } from "vitest";
import "@/tests";
import type { PluginInstallation, PluginJob } from "@/apis/raw/plugins";
import { installation } from "./fixtures";
import { installedRows } from "./model";
import {
  attentionCause,
  enabledDependents,
  openJobs,
  pluginStatus,
  runtimeLine,
} from "./status";

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
      name: "an update job is activating",
      item: base,
      jobs: [job("update")],
      expected: "activating",
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
  it("counts only the dependents that are switched on", () => {
    const needed = installation(
      "engine",
      "translator",
      { provides: ["engine.v1"] },
      { name: "Engine" },
    );
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
    const all = [needed, on, off];
    const row = rowFor(needed, [on, off]);
    expect(row.dependents).toEqual(["Backend off", "Backend on"]);
    expect(enabledDependents(row, all)).toEqual(["Backend on"]);
  });
});
