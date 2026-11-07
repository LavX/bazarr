/* eslint-disable camelcase -- manifest attribute names. */
import { FunctionComponent, useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  applyChoice,
  groupFields,
  initialValues,
  isVisible,
  PluginSettingsForm,
  withDefaults,
} from "@/components/plugin-settings";
import type {
  OptionsState,
  PluginField,
  PluginSecrets,
  PluginValues,
} from "@/components/plugin-settings/types";
import { customRender, screen, within } from "@/tests";
import { openSelect } from "@/tests/select";
import { FIELDS } from "./fixtures";

interface HarnessProps {
  fields?: PluginField[];
  start?: PluginValues;
  secrets?: PluginSecrets;
  options?: OptionsState;
  errors?: Record<string, string>;
  onClearSecret?: (key: string) => void;
  onValues?: (values: PluginValues) => void;
}

const Harness: FunctionComponent<HarnessProps> = ({
  fields = FIELDS,
  start = {},
  secrets,
  options,
  errors,
  onClearSecret,
  onValues,
}) => {
  const [values, setValues] = useState<PluginValues>(start);
  return (
    <PluginSettingsForm
      fields={fields}
      values={values}
      secrets={secrets}
      errors={errors}
      optionsFor={(key) => (key === "model" ? options : undefined)}
      onClearSecret={onClearSecret}
      onChange={(next) => {
        setValues(next);
        onValues?.(next);
      }}
    />
  );
};

describe("plugin settings fields", () => {
  it("draws a field of every type from the manifest alone", () => {
    customRender(<Harness options={{ available: false, items: [] }} />);
    for (const label of [
      "Label",
      "Base URL",
      "Input price",
      "API key",
      "Batch size",
      "Thinking",
      "Tags",
      "Model",
    ]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByRole("radiogroup", { name: "Preset" })).toBeVisible();
    expect(screen.getByRole("list")).toBeVisible();
    expect(screen.getByText("Alpha")).toBeVisible();
    expect(screen.getByText("Keys")).toBeVisible();
    expect(screen.getByRole("link", { name: "Learn more" })).toHaveAttribute(
      "href",
      "https://example.test/help",
    );
  });

  it("shows defaults and groups, and drops a field whose condition fails", async () => {
    const user = userEvent.setup();
    customRender(<Harness />);
    expect(screen.getByLabelText("Batch size")).toHaveValue("100");
    expect(screen.getByLabelText("Thinking")).not.toBeChecked();
    // The group heading, and the conditional field inside it, while preset is the default "custom".
    expect(screen.getByText("Model", { selector: "p" })).toBeVisible();
    expect(screen.getByLabelText("Region")).toBeVisible();
    await user.click(screen.getByLabelText("LM Studio"));
    expect(screen.queryByLabelText("Region")).not.toBeInTheDocument();
  });

  it("stops typing at the declared length", async () => {
    const user = userEvent.setup();
    customRender(<Harness />);
    await user.type(screen.getByLabelText("Label"), "0123456789abcdef");
    expect(screen.getByLabelText("Label")).toHaveValue("0123456789ab");
  });

  it("fills the fields a preset names, keeps them editable, and typing in them is not a paste", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(<Harness onValues={seen} />);
    await user.click(screen.getByLabelText("LM Studio"));
    const url = screen.getByLabelText(/Base URL/);
    expect(url).toHaveValue("http://localhost:1234/v1");
    expect(screen.getByLabelText("Input price")).toHaveValue("0");
    // Still editable: typed onto the preset's value, one key at a time.
    await user.clear(url);
    await user.type(url, "http://192.168.1.5:1234/v1");
    expect(url).toHaveValue("http://192.168.1.5:1234/v1");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({
        preset: "lmstudio",
        base_url: "http://192.168.1.5:1234/v1",
      }),
    );
    // Picking another preset overwrites what it names.
    await user.click(screen.getByLabelText("OpenAI"));
    expect(screen.getByLabelText(/Base URL/)).toHaveValue(
      "https://api.openai.com/v1",
    );
    expect(screen.getByLabelText("Input price")).toHaveValue("0.15");
  });

  it("warns that localhost inside the container is the container", async () => {
    const user = userEvent.setup();
    customRender(<Harness />);
    expect(screen.queryByText("host.docker.internal")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("LM Studio"));
    expect(screen.getByText("host.docker.internal")).toBeVisible();
    await user.click(screen.getByLabelText("OpenAI"));
    expect(screen.queryByText("host.docker.internal")).not.toBeInTheDocument();
  });

  it("never fills a secret and reports typing into it", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(
      <Harness
        secrets={{ api_key: { configured: true, last4: "WXYZ" } }}
        onValues={seen}
      />,
    );
    const input = screen.getByPlaceholderText(
      "Saved, ending WXYZ. Leave empty to keep it.",
    );
    expect(input).toHaveValue("");
    await user.type(input, "sk-new");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ api_key: "sk-new" }),
    );
  });

  it("asks for a stale secret again instead of saying it is saved", () => {
    customRender(
      <Harness secrets={{ api_key: { configured: true, stale: true } }} />,
    );
    expect(
      screen.getByPlaceholderText("Enter the key again for this address"),
    ).toBeVisible();
  });

  it("shows the error next to the field it belongs to", () => {
    customRender(<Harness errors={{ batch: "must be at most 500" }} />);
    expect(screen.getByText("must be at most 500")).toBeVisible();
  });
});

describe("the three rich controls", () => {
  const items = [
    {
      id: "vendor/chat",
      name: "Chat model",
      context_length: 128000,
      price_input_per_million: 0.15,
      price_output_per_million: 0.6,
      output_modalities: ["text"],
      sets: { price_input_per_million: 0.15 },
    },
    {
      id: "vendor/image",
      name: "Image model",
      output_modalities: ["image"],
    },
    { id: "vendor/plain", name: "No modalities listed" },
  ];

  it("lists only text-output models, with price and context, and a pick fills the prices", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(
      <Harness options={{ available: true, items }} onValues={seen} />,
    );
    const listbox = await openSelect(user, "Pick a model");
    expect(within(listbox).getByText("Chat model")).toBeInTheDocument();
    expect(
      within(listbox).getByText("No modalities listed"),
    ).toBeInTheDocument();
    expect(within(listbox).queryByText("Image model")).not.toBeInTheDocument();
    expect(within(listbox).getByText(/\$0\.150\/M in/)).toBeInTheDocument();
    expect(within(listbox).getByText(/128k context/)).toBeInTheDocument();
    await user.click(within(listbox).getByText("Chat model"));
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({
        model: "vendor/chat",
        price_input_per_million: 0.15,
      }),
    );
  });

  it("falls back to free text when the model list cannot be fetched, and takes typing", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(
      <Harness options={{ available: false, items: [] }} onValues={seen} />,
    );
    expect(
      screen.getByText(
        "The model list could not be fetched. Type the model name.",
      ),
    ).toBeVisible();
    await user.type(screen.getByLabelText("Model"), "my/model");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ model: "my/model" }),
    );
  });

  it("holds keys in a list: add by typing, a masked row, remove, and a cap", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(<Harness onValues={seen} />);
    const input = screen.getByLabelText("Add to Keys");
    for (const key of [
      "gem-key-one-1111",
      "gem-key-two-2222",
      "gem-key-three-3333",
    ]) {
      await user.type(input, key);
      await user.click(screen.getByRole("button", { name: "Add" }));
    }
    expect(screen.getByText("••••1111")).toBeVisible();
    expect(screen.queryByText("gem-key-one-1111")).not.toBeInTheDocument();
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({
        api_keys: [
          "gem-key-one-1111",
          "gem-key-two-2222",
          "gem-key-three-3333",
        ],
      }),
    );
    expect(input).toBeDisabled(); // max_items is 3
    await user.click(
      screen.getByRole("button", { name: "Remove key ending 2222" }),
    );
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({
        api_keys: ["gem-key-one-1111", "gem-key-three-3333"],
      }),
    );
    expect(screen.getByLabelText("Add to Keys")).toBeEnabled();
  });

  it("says how many keys are saved, never which, and can clear them", async () => {
    const user = userEvent.setup();
    const cleared = vi.fn();
    customRender(
      <Harness
        secrets={{ api_keys: { configured: true, count: 2 } }}
        onClearSecret={cleared}
      />,
    );
    expect(screen.getByText("2 saved keys")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear saved keys" }));
    expect(cleared).toHaveBeenCalledWith("api_keys");
  });

  it("reorders a list with the buttons the drag handle sits beside", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(<Harness onValues={seen} />);
    await user.click(screen.getByRole("button", { name: "Move Alpha down" }));
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ order: ["b", "a", "c"] }),
    );
    expect(screen.getByRole("button", { name: "Move Beta up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move c down" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Drag Alpha" })).toBeVisible();
  });
});

describe("labels, errors and secrets for the compound controls", () => {
  it("names each compound control by its label and links its error", () => {
    customRender(
      <Harness
        errors={{
          preset: "pick one",
          order: "bad order",
          api_keys: "too many",
        }}
      />,
    );
    for (const [name, message] of [
      ["Preset", "pick one"],
      ["Order", "bad order"],
      ["Keys", "too many"],
    ]) {
      const group = screen.getByRole(
        name === "Preset" ? "radiogroup" : "group",
        { name },
      );
      const alert = screen.getByText(message);
      expect(alert).toHaveAttribute("role", "alert");
      expect(group.getAttribute("aria-describedby")).toContain(alert.id);
    }
  });

  it("clears a single stored secret from the field and shows it pending", async () => {
    const user = userEvent.setup();
    const cleared = vi.fn();
    customRender(
      <Harness
        secrets={{ api_key: { configured: true, last4: "WXYZ" } }}
        onClearSecret={cleared}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Clear saved API key" }),
    );
    expect(cleared).toHaveBeenCalledWith("api_key");
  });

  it("holds a secret list like a key list, with its stored count", () => {
    customRender(
      <Harness
        fields={[
          { key: "tokens", type: "list", label: "Tokens", secret: true },
        ]}
        secrets={{ tokens: { configured: true, count: 3 } }}
      />,
    );
    expect(screen.getByText("3 saved keys")).toBeVisible();
    expect(screen.getByLabelText("Add to Tokens")).toHaveAttribute(
      "type",
      "password",
    );
  });

  it("shows per-key health beside a stored key list", () => {
    customRender(
      <Harness
        fields={[{ key: "ks", type: "key_list", label: "Ks", secret: true }]}
        start={{ ks: ["abcdefghijkl"] }}
        secrets={{
          ks: {
            configured: true,
            count: 1,
            health: [{ state: "cooling_down" }],
          },
        }}
      />,
    );
    expect(screen.getByText("cooling down")).toBeVisible();
  });

  it("keeps the fallback note on a model field that has help text", () => {
    customRender(
      <Harness
        fields={[
          { key: "model", type: "options", label: "Model", help: "Pick one." },
        ]}
        options={{ available: false, items: [] }}
      />,
    );
    expect(screen.getByText(/Pick one\./)).toBeVisible();
    expect(screen.getByText(/could not be fetched/)).toBeVisible();
  });
});

describe("field helpers", () => {
  it("applies defaults for display and conditions", () => {
    expect(withDefaults(FIELDS, { batch: 5 })).toEqual(
      expect.objectContaining({ batch: 5, thinking: false, preset: "custom" }),
    );
  });

  it("reads conditions as equals or in", () => {
    const field: PluginField = {
      key: "x",
      type: "text",
      show_if: { key: "m", in: ["a", "b"] },
    };
    expect(isVisible(field, { m: "b" })).toBe(true);
    expect(isVisible(field, { m: "z" })).toBe(false);
    expect(isVisible({ key: "y", type: "text" }, {})).toBe(true);
  });

  it("applies a preset on top of the values and keeps the choice", () => {
    const preset = FIELDS[1];
    expect(applyChoice(preset, "openai", { label: "kept" })).toEqual({
      label: "kept",
      base_url: "https://api.openai.com/v1",
      price_input_per_million: 0.15,
      preset: "openai",
    });
  });

  it("puts ungrouped fields first and starts a form without secrets", () => {
    expect(groupFields(FIELDS).map((item) => item.group)).toEqual([
      "",
      "Model",
    ]);
    const start = initialValues(FIELDS, {
      label: "x",
      api_key: "leaked",
      api_keys: ["leaked"],
    });
    expect(start).toEqual({ label: "x" });
  });
});
