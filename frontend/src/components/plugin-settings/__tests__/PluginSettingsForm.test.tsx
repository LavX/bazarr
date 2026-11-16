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
import { cleanup, customRender, screen, within } from "@/tests";
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

  it("keeps a number as typed, character by character, and sends null only for an empty box", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(
      <Harness start={{ price_input_per_million: 40 }} onValues={seen} />,
    );
    const input = screen.getByLabelText("Input price");
    const sent = () =>
      seen.mock.calls.map(
        ([values]) => (values as PluginValues).price_input_per_million,
      );
    const last = () => sent().at(-1);

    await user.clear(input);
    expect(last()).toBeNull();
    // JSON carries a null; it would have dropped an undefined.
    expect(JSON.parse(JSON.stringify(seen.mock.lastCall?.[0]))).toHaveProperty(
      "price_input_per_million",
      null,
    );

    seen.mockClear();
    await user.type(input, "0.15");
    expect(input).toHaveValue("0.15");
    expect(last()).toBe(0.15);
    // Never the 15 or the null that a half-typed "0." would have become.
    expect(new Set(sent())).toEqual(new Set([0, 0.1, 0.15]));

    await user.clear(input);
    seen.mockClear();
    await user.type(input, "1.");
    expect(input).toHaveValue("1.");
    expect(last()).toBe(1);
    await user.type(input, "5");
    expect(input).toHaveValue("1.5");
    expect(last()).toBe(1.5);

    await user.clear(input);
    seen.mockClear();
    await user.type(input, "-");
    expect(
      sent().every((value) => value === null || Number.isFinite(value)),
    ).toBe(true);
    expect(last() ?? null).toBeNull();

    await user.clear(input);
    seen.mockClear();
    await user.type(input, "0.0001");
    expect(input).toHaveValue("0.0001");
    expect(last()).toBe(0.0001);
    expect(
      sent().every(
        (value) =>
          value === 0 ||
          value === 0.0001 ||
          value === 0.00001 ||
          (value as number) < 0.001,
      ),
    ).toBe(true);

    await user.clear(input);
    expect(last()).toBeNull();
  });

  it("keeps a half-typed minus on screen when a number is replaced by a negative one", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    const fields: PluginField[] = [
      { key: "offset", type: "number", label: "Offset" },
    ];
    customRender(
      <Harness fields={fields} start={{ offset: 40 }} onValues={seen} />,
    );
    const input = screen.getByLabelText("Offset");
    // Select the existing 40 and type over it, one key at a time.
    await user.type(input, "-", {
      initialSelectionStart: 0,
      initialSelectionEnd: 2,
    });
    expect(input).toHaveValue("-");
    expect(seen).not.toHaveBeenCalledWith(
      expect.objectContaining({ offset: 40 }),
    );
    await user.type(input, "5");
    expect(input).toHaveValue("-5");
    expect(seen).toHaveBeenLastCalledWith({ offset: -5 });
    // Replacing it again with another negative number works the same way.
    await user.type(input, "-7", {
      initialSelectionStart: 0,
      initialSelectionEnd: 2,
    });
    expect(input).toHaveValue("-7");
    expect(seen).toHaveBeenLastCalledWith({ offset: -7 });
  });

  it("keeps the default visible as a hint while an emptied number is null", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(<Harness start={{ batch: 40 }} onValues={seen} />);
    const input = screen.getByLabelText("Batch size");
    await user.clear(input);
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "100");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ batch: null }),
    );
    await user.type(input, "7");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ batch: 7 }),
    );
  });

  it("takes text, a free-text model, tags and keys exactly as typed", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(
      <Harness
        start={{ preset: "custom" }}
        options={{ available: false, items: [] }}
        onValues={seen}
      />,
    );
    const last = () => seen.mock.lastCall?.[0] as PluginValues;
    await user.type(screen.getByLabelText("Label"), "Ab 1.");
    expect(screen.getByLabelText("Label")).toHaveValue("Ab 1.");
    expect(last().label).toBe("Ab 1.");
    await user.type(screen.getByLabelText("Model"), "vendor/m-1.5:free");
    expect(last().model).toBe("vendor/m-1.5:free");
    await user.type(
      screen
        .getAllByLabelText("Tags")
        .find(
          (item) =>
            item.tagName === "INPUT" && item.getAttribute("type") !== "hidden",
        )!,
      "one{Enter}t.w.o{Enter}",
    );
    expect(last().tags).toEqual(["one", "t.w.o"]);
    await user.type(screen.getByLabelText("Add to Keys"), "key-0.9{Enter}");
    expect(last().api_keys).toEqual(["key-0.9"]);
  });

  it("reads an emptied field as its default when a condition depends on it, and still sends null", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    customRender(<Harness start={{ preset: "lmstudio" }} onValues={seen} />);
    expect(screen.queryByLabelText("Region")).not.toBeInTheDocument();
    cleanup();
    // The preset is emptied to null: the default ("custom") applies, so its dependent field shows.
    customRender(<Harness start={{ preset: null }} onValues={seen} />);
    expect(screen.getByLabelText("Region")).toBeVisible();
    expect(withDefaults(FIELDS, { preset: null }).preset).toBe("custom");
    expect(
      isVisible(
        FIELDS.find((f) => f.key === "region")!,
        withDefaults(FIELDS, { preset: null }),
      ),
    ).toBe(true);
    // The payload keeps the null while the user edits another field.
    await user.type(screen.getByLabelText("Label"), "x");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ preset: null, label: "x" }),
    );
  });

  it("hides and shows a dependent field as a controlling text field is typed and cleared", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    const fields: PluginField[] = [
      { key: "mode", type: "text", label: "Mode", default: "auto" },
      {
        key: "extra",
        type: "text",
        label: "Extra",
        required: true,
        show_if: { key: "mode", equals: "auto" },
      },
    ];
    customRender(<Harness fields={fields} onValues={seen} />);
    const mode = screen.getByLabelText("Mode");
    expect(screen.getByLabelText(/Extra/)).toBeVisible();
    await user.clear(mode);
    await user.type(mode, "m");
    expect(screen.queryByLabelText(/Extra/)).not.toBeInTheDocument();
    // Emptied, the box is "no value": the API applies the default "auto", so Extra applies again.
    await user.clear(mode);
    expect(screen.getByLabelText(/Extra/)).toBeVisible();
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ mode: "" }),
    );
  });

  it("applies the server's empty-value rules to every type when conditions are read", () => {
    const fields: PluginField[] = [
      { key: "name", type: "text", default: "d" },
      { key: "tags", type: "list", default: ["x"] },
      { key: "size", type: "integer", default: 5 },
      { key: "flag", type: "toggle", default: true },
    ];
    expect(
      withDefaults(fields, { name: "", tags: [], size: null, flag: false }),
    ).toEqual({ name: "d", tags: ["x"], size: 5, flag: false });
    expect(
      withDefaults(fields, { name: "n", tags: ["t"], size: 0, flag: true }),
    ).toEqual({ name: "n", tags: ["t"], size: 0, flag: true });
  });

  it("shows an options field's manifest default until a value is saved", async () => {
    const user = userEvent.setup();
    const seen = vi.fn();
    const fields: PluginField[] = [
      {
        key: "model",
        type: "options",
        label: "Model",
        default: "vendor/default",
      },
    ];
    const { unmount } = customRender(
      <Harness
        fields={fields}
        options={{ available: false, items: [] }}
        onValues={seen}
      />,
    );
    expect(screen.getByLabelText("Model")).toHaveValue("vendor/default");
    await user.type(screen.getByLabelText("Model"), "x");
    expect(seen).toHaveBeenLastCalledWith({ model: "vendor/defaultx" });
    unmount();
    customRender(
      <Harness
        fields={fields}
        start={{ model: "vendor/mine" }}
        options={{ available: false, items: [] }}
      />,
    );
    expect(screen.getByLabelText("Model")).toHaveValue("vendor/mine");
    cleanup();
    // With a live list the picker selects the default, which the list may or may not offer.
    customRender(
      <Harness
        fields={fields}
        options={{
          available: true,
          items: [{ id: "vendor/default", name: "The default" }],
        }}
      />,
    );
    expect(screen.getByDisplayValue("The default")).toBeInTheDocument();
  });

  it("starts an unset reorderable list as the order it shows, so the draft holds what is displayed", () => {
    expect(initialValues(FIELDS, {}).order).toEqual(["a", "b", "c"]);
    expect(initialValues(FIELDS, { order: ["c", "a"] }).order).toEqual([
      "c",
      "a",
    ]);
    const withDefault: PluginField[] = [
      {
        key: "order",
        type: "orderable_list",
        items: ["a", "b"],
        default: ["b", "a"],
      },
    ];
    expect(initialValues(withDefault, {}).order).toEqual(["b", "a"]);
    // A secret field never starts with a value.
    expect(initialValues([{ key: "k", type: "secret" }], { k: "x" })).toEqual(
      {},
    );
  });

  it("compares a list in a condition by its members, not by identity", () => {
    const field: PluginField = {
      key: "extra",
      type: "text",
      show_if: { key: "tags", equals: ["x"] },
    };
    expect(isVisible(field, { tags: ["x"] })).toBe(true);
    expect(isVisible(field, { tags: ["x", "y"] })).toBe(false);
    expect(isVisible(field, { tags: ["y"] })).toBe(false);
    expect(isVisible(field, { tags: "x" })).toBe(false);
    const among: PluginField = {
      key: "extra",
      type: "text",
      show_if: { key: "tags", in: [["a"], ["x", "y"]] },
    };
    expect(isVisible(among, { tags: ["x", "y"] })).toBe(true);
    expect(isVisible(among, { tags: ["y", "x"] })).toBe(false);
  });

  it("shows a field whose condition equals a list when the typed list matches", async () => {
    const user = userEvent.setup();
    const fields: PluginField[] = [
      { key: "tags", type: "list", label: "Tags" },
      {
        key: "extra",
        type: "text",
        label: "Extra",
        show_if: { key: "tags", equals: ["x"] },
      },
    ];
    customRender(<Harness fields={fields} />);
    expect(screen.queryByLabelText("Extra")).not.toBeInTheDocument();
    await user.type(
      screen
        .getAllByLabelText("Tags")
        .find(
          (item) =>
            item.tagName === "INPUT" && item.getAttribute("type") !== "hidden",
        )!,
      "x{Enter}",
    );
    expect(screen.getByLabelText("Extra")).toBeVisible();
  });

  it("draws a text field marked secret as a masked secret with its stored status and a clear button", async () => {
    const user = userEvent.setup();
    const cleared = vi.fn();
    const seen = vi.fn();
    const fields: PluginField[] = [
      { key: "token", type: "text", secret: true, label: "Token" },
    ];
    customRender(
      <Harness
        fields={fields}
        secrets={{ token: { configured: true, last4: "abcd" } }}
        onClearSecret={cleared}
        onValues={seen}
      />,
    );
    const input = screen.getByLabelText("Token");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute(
      "placeholder",
      "Saved, ending abcd. Leave empty to keep it.",
    );
    await user.type(input, "typed-secret");
    expect(seen).toHaveBeenLastCalledWith({ token: "typed-secret" });
    expect(input).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Clear saved Token" }));
    expect(cleared).toHaveBeenCalledWith("token");
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
    await user.type(url, "http://translator.example:1234/v1");
    expect(url).toHaveValue("http://translator.example:1234/v1");
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({
        preset: "lmstudio",
        base_url: "http://translator.example:1234/v1",
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
    // The reorderable list has no stored value, so it starts as the order it is shown in.
    expect(start).toEqual({ label: "x", order: ["a", "b", "c"] });
  });
});
