import { FunctionComponent } from "react";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import {
  FormContext,
  type FormValues,
} from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { customRender, screen, waitFor } from "@/tests";
import AIModelSelector from "./AIModelSelector";

const MODEL_KEY = "settings-translator-openrouter_model";
const ROUTING_KEY = "settings-translator-openrouter_provider_routing";

// Renders the selector with a real settings form behind it and exposes the
// staged values so a test can assert what the form would submit.
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
  it("moves a typed :floor into the provider routing setting", async () => {
    const user = userEvent.setup();
    const stagedValues = mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("z-ai/glm-5.3-flash:floor");

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

    await waitFor(() => {
      expect(stagedValues[MODEL_KEY]).toBe("deepseek/deepseek-chat:thinking");
    });
    expect(stagedValues[ROUTING_KEY]).toBeUndefined();
  });

  it("tells the user the routing was adopted", async () => {
    const user = userEvent.setup();
    mountSelector();

    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.paste("z-ai/glm-5.3-flash:floor");

    expect(
      await screen.findByText(/moved .*:floor.* into provider routing/i),
    ).toBeInTheDocument();
  });
});
