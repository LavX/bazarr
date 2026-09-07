import { FunctionComponent } from "react";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import {
  FormContext,
  type FormValues,
  useFormActions,
} from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { customRender, screen, waitFor } from "@/tests";
import AIModelSelector from "./AIModelSelector";

const MODEL_KEY = "settings-translator-openrouter_model";
const ROUTING_KEY = "settings-translator-openrouter_provider_routing";

// Renders the selector with a real settings form behind it and exposes the
// staged values so a test can assert what the form would submit.
// Stands in for the Provider Routing selector, which lives elsewhere on the
// page: clicking it changes the routing setting the way that selector would.
function RoutingSetter() {
  const { setValue } = useFormActions();

  return (
    <button type="button" onClick={() => setValue("throughput", ROUTING_KEY)}>
      Set routing to throughput
    </button>
  );
}

const Harness: FunctionComponent<{ onValues: (v: LooseObject) => void }> = ({
  onValues,
}) => {
  const mantineForm = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  onValues(mantineForm.values.settings);
  return (
    <SettingsProvider
      value={
        {
          translator: {
            openrouter_model: "z-ai/glm-5.3-flash",
            openrouter_provider_routing: "throughput",
          },
        } as unknown as Settings
      }
    >
      <FormContext.Provider value={mantineForm}>
        <AIModelSelector />
        <RoutingSetter />
      </FormContext.Provider>
    </SettingsProvider>
  );
};

// Returns the live staged settings the form would submit, refreshed on render.
function mountSelector() {
  const stagedValues: LooseObject = {};
  customRender(
    <Harness
      onValues={(v) => {
        Object.keys(stagedValues).forEach((k) => delete stagedValues[k]);
        Object.assign(stagedValues, v);
      }}
    />,
  );
  return stagedValues;
}

describe("AIModelSelector routing adoption", () => {
  it("does not adopt half a word while the user is still typing", async () => {
    // ":floor" is an exact suffix in the middle of typing ":floorplan", so
    // adopting on every keystroke would eat the model id.
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.type(input, "some/model:floorplan");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("some/model:floorplan");
    });
    expect(stagedValues[ROUTING_KEY]).toBeUndefined();
  });

  it("keeps a typo intact rather than adopting the prefix", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.type(input, "some/model:floorr");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("some/model:floorr");
    });
    expect(stagedValues[ROUTING_KEY]).toBeUndefined();
  });

  it("adopts a suffix typed one character at a time once the field is left", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.type(input, "z-ai/glm-5.3-flash:nitro");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("z-ai/glm-5.3-flash");
      expect(stagedValues[ROUTING_KEY]).toBe("nitro");
    });
  });

  it("moves a typed :floor into the provider routing setting", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("z-ai/glm-5.3-flash:floor");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("z-ai/glm-5.3-flash");
      expect(stagedValues[ROUTING_KEY]).toBe("floor");
    });
  });

  it("moves a typed :nitro into the provider routing setting", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("deepseek/deepseek-v4-flash:nitro");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("deepseek/deepseek-v4-flash");
      expect(stagedValues[ROUTING_KEY]).toBe("nitro");
    });
  });

  it("leaves another variant on the model and does not touch the routing", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("deepseek/deepseek-chat:thinking");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("deepseek/deepseek-chat:thinking");
    });
    expect(stagedValues[ROUTING_KEY]).toBeUndefined();
  });

  it("trims a pasted model id even when it carries no routing suffix", async () => {
    // The details lookup trims, so an untrimmed id would show a valid card
    // while the saved setting, and the request built from it, keep the spaces.
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("  z-ai/glm-5.3-flash  ");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("z-ai/glm-5.3-flash");
    });
    expect(stagedValues[ROUTING_KEY]).toBeUndefined();
  });

  it("takes every stacked routing suffix off the model id", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("z-ai/glm-5.3-flash:nitro:floor");
    await user.tab();

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("z-ai/glm-5.3-flash");
      expect(stagedValues[ROUTING_KEY]).toBe("floor");
    });
  });

  it("drops the notice once the routing is changed by hand", async () => {
    const user = userEvent.setup();
    mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("z-ai/glm-5.3-flash:floor");
    await user.tab();
    expect(
      await screen.findByText(/moved .*:floor.* into provider routing/i),
    ).toBeInTheDocument();

    // The routing selector lives elsewhere on the page; changing the setting is
    // what the notice has to react to.
    await user.click(
      screen.getByRole("button", { name: "Set routing to throughput" }),
    );

    await waitFor(() => {
      expect(
        screen.queryByText(/moved .*into provider routing/i),
      ).not.toBeInTheDocument();
    });
  });

  it("tells the user the routing was adopted", async () => {
    const user = userEvent.setup();
    mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("z-ai/glm-5.3-flash:floor");
    await user.tab();

    expect(
      await screen.findByText(/moved .*:floor.* into provider routing/i),
    ).toBeInTheDocument();
  });
});
