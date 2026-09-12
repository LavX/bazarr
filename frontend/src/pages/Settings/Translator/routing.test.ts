import { describe, expect, it } from "vitest";
import {
  aiTranslatorProviderRoutingOptions,
  routingLabel,
  splitRoutingSuffix,
} from "./routing";

describe("splitRoutingSuffix", () => {
  it("leaves a plain model id alone", () => {
    expect(splitRoutingSuffix("z-ai/glm-5.3-flash")).toEqual({
      modelId: "z-ai/glm-5.3-flash",
      routing: null,
    });
  });

  it("takes :floor off the model id", () => {
    expect(splitRoutingSuffix("z-ai/glm-5.3-flash:floor")).toEqual({
      modelId: "z-ai/glm-5.3-flash",
      routing: "floor",
    });
  });

  it("takes :nitro off the model id", () => {
    expect(splitRoutingSuffix("deepseek/deepseek-v4-flash:nitro")).toEqual({
      modelId: "deepseek/deepseek-v4-flash",
      routing: "nitro",
    });
  });

  it("keeps other variants, which are part of the model", () => {
    for (const id of [
      "deepseek/deepseek-chat:thinking",
      "liquid/lfm-2.5-2.6b:free",
      "z-ai/glm-5.3-flash:batch",
      "openai/gpt-5.2:online",
      "anthropic/claude-haiku-4.5:extended",
      "x-ai/grok-4.1:exacto",
    ]) {
      expect(splitRoutingSuffix(id)).toEqual({ modelId: id, routing: null });
    }
  });

  it("takes the routing suffix off a model that also carries another variant", () => {
    expect(splitRoutingSuffix("liquid/lfm-2.5-2.6b:free:nitro")).toEqual({
      modelId: "liquid/lfm-2.5-2.6b:free",
      routing: "nitro",
    });
  });

  it("is case insensitive and tolerates surrounding whitespace", () => {
    expect(splitRoutingSuffix("  z-ai/glm-5.3-flash:FLOOR ")).toEqual({
      modelId: "z-ai/glm-5.3-flash",
      routing: "floor",
    });
  });

  it("takes every stacked routing suffix off and keeps the last one typed", () => {
    // Leaving one behind would contradict the selector: the backend reads the
    // suffix off the model id and lets it win.
    expect(splitRoutingSuffix("z-ai/glm-5.3-flash:nitro:floor")).toEqual({
      modelId: "z-ai/glm-5.3-flash",
      routing: "floor",
    });
    expect(splitRoutingSuffix("z-ai/glm-5.3-flash:floor:nitro")).toEqual({
      modelId: "z-ai/glm-5.3-flash",
      routing: "nitro",
    });
  });

  it("keeps a non-routing variant that sits under stacked routing suffixes", () => {
    expect(splitRoutingSuffix("liquid/lfm-2.5-2.6b:free:nitro:floor")).toEqual({
      modelId: "liquid/lfm-2.5-2.6b:free",
      routing: "floor",
    });
  });

  it("does not mistake a lookalike for a routing suffix", () => {
    for (const id of [
      "some/model:floorplan",
      "some/model:nitrox",
      "some/floor",
    ]) {
      expect(splitRoutingSuffix(id).routing).toBeNull();
    }
  });

  it("handles an empty value", () => {
    expect(splitRoutingSuffix("")).toEqual({ modelId: "", routing: null });
  });
});

describe("routingLabel", () => {
  it("names every routing value from the option list", () => {
    for (const option of aiTranslatorProviderRoutingOptions) {
      expect(routingLabel(option.value)).toBe(option.label);
    }
  });

  it("falls back to the raw value it does not know", () => {
    expect(routingLabel("cheapest")).toBe("cheapest");
  });
});

describe("routing shortcuts sitting in front of a variant", () => {
  // The Bazarr backend removes a shortcut from any position. Stopping at the first
  // non-routing tail here left the two disagreeing about which model is requested:
  // the picker looked up one id while the request carried another.
  it.each([
    ["deepseek/example:nitro:free", "deepseek/example:free", "nitro"],
    [
      "deepseek/example:smartfast:thinking",
      "deepseek/example:thinking",
      "smartfast",
    ],
    ["deepseek/example:floor:free:nitro", "deepseek/example:free", "nitro"],
    ["deepseek/example:smartfast:nitro", "deepseek/example", "nitro"],
  ])("splits %s", (raw, modelId, routing) => {
    expect(splitRoutingSuffix(raw)).toEqual({ modelId, routing });
  });

  it("leaves a genuine variant alone", () => {
    expect(splitRoutingSuffix("deepseek/example:free:thinking")).toEqual({
      modelId: "deepseek/example:free:thinking",
      routing: null,
    });
  });
});
