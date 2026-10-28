import { FunctionComponent } from "react";
import { useForm } from "@mantine/form";
import { notifications } from "@mantine/notifications";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TranslatorTestResponse } from "@/apis/hooks/translator";
import {
  FormContext,
  type FormValues,
} from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { customRender, screen } from "@/tests";
import { TestConnectionButton } from "./index";
import {
  aiTranslatorConcurrentOptions,
  aiTranslatorModelOptions,
  aiTranslatorReasoningOptions,
  translatorOption,
} from "./options";
import { aiTranslatorProviderRoutingOptions } from "./routing";

describe("Translator options", () => {
  it("exports all required option arrays", () => {
    expect(translatorOption).toBeDefined();
    expect(translatorOption.length).toBeGreaterThan(0);
    expect(
      translatorOption.find((o) => o.value === "openrouter"),
    ).toBeDefined();

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

  it("provider routing options match the backend setting values", () => {
    const values = aiTranslatorProviderRoutingOptions.map((o) => o.value);
    expect(values).toEqual([
      "smartfast",
      "custom",
      "throughput",
      "nitro",
      "price",
      "floor",
      "latency",
      "default",
    ]);
  });

  it("provider routing marks smartfast as the default, and only smartfast", () => {
    // The selector is where a user learns what an untouched install does, so exactly
    // one option may claim it and it has to be the one the backend validator ships.
    const labelled = aiTranslatorProviderRoutingOptions.filter((o) =>
      o.label.includes("Default"),
    );
    expect(labelled.map((o) => o.value)).toEqual(["smartfast"]);
  });
});

const mutate = vi.fn();

// Render the button through its settings form and control request outcomes.
vi.mock("@/apis/hooks/translator", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/apis/hooks/translator")>()),
  useTestTranslator: () => ({ mutate, isPending: false }),
}));

// The loaded configuration the page would hand the button. `settings` stands
// in for the saved values, so a row can hold the gate open with a service URL
// and no Bazarr-side key.
const TestButtonHarness: FunctionComponent<{ settings: LooseObject }> = ({
  settings,
}) => {
  const mantineForm = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });

  return (
    <SettingsProvider value={settings as unknown as Settings}>
      <FormContext.Provider value={mantineForm}>
        <TestConnectionButton />
      </FormContext.Provider>
    </SettingsProvider>
  );
};

describe("Translator Test Connection button", () => {
  beforeEach(() => {
    mutate.mockReset();
    vi.spyOn(notifications, "show").mockClear();
  });

  it("shows the refusal reason once when the connection test fails", async () => {
    mutate.mockImplementation(
      (_params: unknown, options: { onError?: (error: unknown) => void }) => {
        options.onError?.({ response: { data: { error: "reason text" } } });
      },
    );
    customRender(
      <TestButtonHarness
        settings={{
          translator: { ["openrouter_url"]: "http://localhost:8990" },
        }}
      />,
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /test connection/i }));

    expect(notifications.show).toHaveBeenCalledTimes(1);
    expect(notifications.show).toHaveBeenCalledWith({
      title: "Connection Failed",
      message: "reason text",
      color: "red",
    });
  });

  it("explains a service-held API key without claiming it was validated", async () => {
    mutate.mockImplementation(
      (
        _params: unknown,
        options: {
          onSuccess?: (data: TranslatorTestResponse) => void;
        },
      ) => {
        options.onSuccess?.({ keyHeldByService: true, message: "held" });
      },
    );
    customRender(
      <TestButtonHarness
        settings={{
          translator: { ["openrouter_url"]: "http://localhost:8990" },
        }}
      />,
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /test connection/i }));

    expect(notifications.show).toHaveBeenCalledTimes(1);
    expect(notifications.show).toHaveBeenCalledWith({
      title: "API Key",
      message: "held",
      color: "blue",
    });
    expect(notifications.show).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Connected" }),
    );
  });

  it("tests an open gate with no Bazarr-side API key, because the sidecar can hold its own", async () => {
    // The availability gate treats the service URL as the only thing Bazarr
    // must supply for the OpenRouter engine. With the sidecar holding its
    // own key, an empty Bazarr-side key must not stop the button from
    // testing, or the gate is open while the button refuses to test.
    customRender(
      <TestButtonHarness
        settings={{
          translator: {
            openrouter_url: "http://localhost:8990",
            openrouter_api_key: "",
          },
        }}
      />,
    );

    const button = screen.getByRole("button", { name: /test connection/i });
    expect(button).toBeEnabled();

    const user = userEvent.setup();
    await user.click(button);

    // The call carries the configured service URL. The Bazarr-side key is
    // passed through as the empty string the field holds, and the test
    // endpoint treats an empty key as absent, so nothing here pins it.
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toMatchObject({
      serviceUrl: "http://localhost:8990",
    });
  });

  it("stays disabled while the service URL is missing, the one thing Bazarr cannot supply itself", async () => {
    customRender(
      <TestButtonHarness
        settings={{
          translator: {
            openrouter_url: "",
            openrouter_api_key: "sk-or-v1-bazarr-side-key",
          },
        }}
      />,
    );

    const button = screen.getByRole("button", { name: /test connection/i });
    expect(button).toBeDisabled();

    const user = userEvent.setup();
    await user.click(button);

    expect(mutate).not.toHaveBeenCalled();
  });
});
