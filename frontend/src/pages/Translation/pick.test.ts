import { describe, expect, it } from "vitest";
import type { TranslationState } from "@/types/translation";
import { connection, engine, lingarr, second } from "./fixtures";
import { ENGINES_GROUP, pickableChoices } from "./model";

const state = (
  ...instances: TranslationState["instances"]
): TranslationState => ({
  enabled: true,
  engine: null,
  fallback_engine: null,
  instances,
});

describe("pickableChoices", () => {
  it("lists a back end that is on, usable and has its key, under its engine", () => {
    expect(
      pickableChoices(state(engine({ connections: [connection(1)] }))),
    ).toEqual([
      {
        key: "connection:1",
        connectionId: 1,
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
      pickableChoices(state(engine({ connections: [connection(1, patch)] }))),
    ).toEqual([]);
  });

  it("leaves out every back end of an engine that is off, waits for its keys or has no running plugin", () => {
    const connections = [connection(1)];
    expect(
      pickableChoices(state(engine({ enabled: false, connections }))),
    ).toEqual([]);
    expect(
      pickableChoices(state(engine({ awaiting_secrets: true, connections }))),
    ).toEqual([]);
    expect(
      pickableChoices(
        state(
          engine({
            plugin: { name: "AI", version: "2.2.0", active: false },
            connections,
          }),
        ),
      ),
    ).toEqual([]);
  });

  it("lists back ends under their engine, the official engine first", () => {
    const other = engine({
      id: 3,
      plugin_id: "other",
      name: "Other Engine",
      official: false,
      connections: [connection(7, { instance_id: 3 })],
    });
    const picks = pickableChoices(
      state(other, engine({ connections: [connection(1)] })),
    );
    expect(picks.map((item) => [item.engine, item.connectionId])).toEqual([
      ["AI Subtitle Translator", 1],
      ["Other Engine", 7],
    ]);
  });

  it("offers an engine with no back ends as a whole, after the back ends", () => {
    expect(
      pickableChoices(state(second, engine({ connections: [connection(1)] }))),
    ).toEqual([
      expect.objectContaining({ key: "connection:1", connectionId: 1 }),
      {
        key: "engine:2",
        engineId: 2,
        label: "Google Translate",
        engine: ENGINES_GROUP,
      },
    ]);
  });

  it("does not offer an engine as a whole while it cannot take a job, or when it has back ends or is legacy", () => {
    // Off, or its plugin does not run.
    expect(
      pickableChoices(state(engine({ ...second, enabled: false }))),
    ).toEqual([]);
    expect(
      pickableChoices(
        state({
          ...second,
          plugin: { name: "Google Translate", version: "1.0.0", active: false },
        }),
      ),
    ).toEqual([]);
    // Not set up.
    expect(pickableChoices(state({ ...second, configured: false }))).toEqual(
      [],
    );
    // An engine that has back ends is picked through them, and Lingarr is used where it always was.
    expect(
      pickableChoices(
        state(engine({ connections: [connection(1, { enabled: false })] })),
      ),
    ).toEqual([]);
    expect(pickableChoices(state(lingarr()))).toEqual([]);
  });
});
