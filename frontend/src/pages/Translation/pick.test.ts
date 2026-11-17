import { describe, expect, it } from "vitest";
import type { TranslationState } from "@/types/translation";
import { connection, engine, second } from "./fixtures";
import { pickableConnections } from "./model";

const state = (
  ...instances: TranslationState["instances"]
): TranslationState => ({
  enabled: true,
  engine: null,
  fallback_engine: null,
  instances,
});

describe("pickableConnections", () => {
  it("lists a back end that is on, usable and has its key, under its engine", () => {
    expect(
      pickableConnections(state(engine({ connections: [connection(1)] }))),
    ).toEqual([
      {
        id: 1,
        label: "OpenAI compatible, model-1",
        engine: "AI Subtitle Translator",
      },
    ]);
  });

  it.each([
    ["is off", { enabled: false }],
    ["is not usable", { usable: false }],
    ["waits for its key", { awaiting_secrets: true }],
  ])("leaves out a back end that %s", (_name, patch) => {
    expect(
      pickableConnections(
        state(engine({ connections: [connection(1, patch)] })),
      ),
    ).toEqual([]);
  });

  it("leaves out every back end of an engine that is off, waits for its keys or has no running plugin", () => {
    const connections = [connection(1)];
    expect(
      pickableConnections(state(engine({ enabled: false, connections }))),
    ).toEqual([]);
    expect(
      pickableConnections(
        state(engine({ awaiting_secrets: true, connections })),
      ),
    ).toEqual([]);
    expect(
      pickableConnections(
        state(
          engine({
            plugin: { name: "AI", version: "2.2.0", active: false },
            connections,
          }),
        ),
      ),
    ).toEqual([]);
  });

  it("has nothing to pick on an engine with no back ends, and lists the official engine first", () => {
    const other = engine({
      id: 3,
      plugin_id: "other",
      name: "Other Engine",
      official: false,
      connections: [connection(7, { instance_id: 3 })],
    });
    const picks = pickableConnections(
      state(other, second, engine({ connections: [connection(1)] })),
    );
    expect(picks.map((item) => [item.engine, item.id])).toEqual([
      ["AI Subtitle Translator", 1],
      ["Other Engine", 7],
    ]);
  });
});
