import { describe, it, expect } from "vitest";
import { customRender, screen } from "@/tests";

// Test that the Translator page module can be imported without errors
describe("Translator settings page", () => {
  it("exports a default component", async () => {
    const module = await import("./index");
    expect(module.default).toBeDefined();
    expect(typeof module.default).toBe("function");
  });
});

describe("Translator options", () => {
  it("exports all required option arrays", async () => {
    const {
      translatorOption,
      aiTranslatorModelOptions,
      aiTranslatorReasoningOptions,
      aiTranslatorConcurrentOptions,
    } = await import("./options");

    expect(translatorOption).toBeDefined();
    expect(translatorOption.length).toBeGreaterThan(0);
    expect(translatorOption.find((o) => o.value === "openrouter")).toBeDefined();

    expect(aiTranslatorModelOptions).toBeDefined();
    expect(aiTranslatorModelOptions.length).toBeGreaterThan(0);

    expect(aiTranslatorReasoningOptions).toBeDefined();
    expect(aiTranslatorReasoningOptions).toContainEqual({
      label: "Disabled",
      value: "disabled",
    });

    expect(aiTranslatorConcurrentOptions).toBeDefined();
    expect(aiTranslatorConcurrentOptions.length).toBe(5);
  });

  it("translator options include all 4 engines", async () => {
    const { translatorOption } = await import("./options");
    const values = translatorOption.map((o) => o.value);
    expect(values).toContain("google_translate");
    expect(values).toContain("gemini");
    expect(values).toContain("lingarr");
    expect(values).toContain("openrouter");
  });
});

describe("AIModelSelector", () => {
  it("exports a default component", async () => {
    const module = await import("./AIModelSelector");
    expect(module.default).toBeDefined();
    expect(typeof module.default).toBe("function");
  });
});
