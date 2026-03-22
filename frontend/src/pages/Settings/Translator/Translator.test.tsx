import { describe, it, expect } from "vitest";
import {
  translatorOption,
  aiTranslatorModelOptions,
  aiTranslatorReasoningOptions,
  aiTranslatorConcurrentOptions,
} from "./options";

describe("Translator options", () => {
  it("exports all required option arrays", () => {
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

  it("includes all 4 translator engines", () => {
    const values = translatorOption.map((o) => o.value);
    expect(values).toContain("google_translate");
    expect(values).toContain("gemini");
    expect(values).toContain("lingarr");
    expect(values).toContain("openrouter");
  });

  it("concurrent options range from 1 to 5", () => {
    const values = aiTranslatorConcurrentOptions.map((o) => o.value);
    expect(values).toEqual([1, 2, 3, 4, 5]);
  });

  it("reasoning options include disabled and high", () => {
    const values = aiTranslatorReasoningOptions.map((o) => o.value);
    expect(values).toContain("disabled");
    expect(values).toContain("high");
  });
});
