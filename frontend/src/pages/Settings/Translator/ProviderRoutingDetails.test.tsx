import { MantineProvider } from "@mantine/core";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FormContext,
  type FormValues,
  useFormActions,
} from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { customRender, screen, waitFor } from "@/tests";
import { parseProviderEndpoints, priceLabel } from "./providerEndpoints";
import ProviderRoutingDetails from "./ProviderRoutingDetails";

const ORDER = "settings-translator-openrouter_provider_order";
const ROUTING = "settings-translator-openrouter_provider_routing";
function SwitchMode() {
  const { setValue } = useFormActions();
  return (
    <>
      <button onClick={() => setValue("smartfast", ROUTING)}>
        Use SmartFast
      </button>
      <button onClick={() => setValue("custom", ROUTING)}>Use Custom</button>
    </>
  );
}
function Harness({
  order = [],
  onValues,
}: {
  order?: string[];
  onValues: (values: LooseObject) => void;
}) {
  const form = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  onValues(form.values.settings);
  return (
    <SettingsProvider
      value={
        {
          translator: {
            openrouter_model: "test/routing-free:free:smartfast",
            openrouter_provider_routing: "custom",
            openrouter_provider_order: order,
          },
        } as unknown as Settings
      }
    >
      <FormContext.Provider value={form}>
        <MantineProvider env="test">
          <ProviderRoutingDetails />
        </MantineProvider>
        <SwitchMode />
      </FormContext.Provider>
    </SettingsProvider>
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("custom provider settings", () => {
  it("selects a single free endpoint, reorders providers and retains choices when switching modes", async () => {
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        data: {
          endpoints: [
            {
              tag: "novita",
              provider_name: "Novita",
              status: 0,
              pricing: { prompt: "0", completion: "0" },
            },
          ],
        },
      }),
    });
    vi.stubGlobal("fetch", fetcher);
    let staged: LooseObject = {};
    customRender(
      <Harness
        onValues={(values) => {
          staged = values;
        }}
      />,
    );
    const user = userEvent.setup();
    expect(
      screen.getByText("Choose at least one provider before translating."),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("combobox", { name: "Add a provider for this model" }),
    );
    await user.click(
      await screen.findByRole("option", {
        name: /Novita.*Input Free, output Free/,
      }),
    );
    await waitFor(() => expect(staged[ORDER]).toEqual(["novita"]));
    expect(fetcher).toHaveBeenCalledWith(
      "https://openrouter.ai/api/v1/models/test/routing-free%3Afree/endpoints",
      expect.objectContaining({ credentials: "omit" }),
    );
    const input = screen.getByRole("combobox", {
      name: "Selected providers (custom slugs)",
    });
    await user.type(input, "deepinfra/turbo{Enter}");
    await user.click(
      screen.getByRole("button", { name: "Move deepinfra/turbo up" }),
    );
    expect(staged[ORDER]).toEqual(["deepinfra/turbo", "novita"]);
    await user.click(screen.getByRole("button", { name: "Use SmartFast" }));
    expect(screen.getByText("SmartFast routing")).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", {
        name: "Selected providers (custom slugs)",
      }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use Custom" }));
    expect(staged[ORDER]).toEqual(["deepinfra/turbo", "novita"]);
  });

  it("preserves saved providers and allows manual entry when metadata fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    let staged: LooseObject = {};
    customRender(
      <Harness
        order={["existing"]}
        onValues={(values) => {
          staged = values;
        }}
      />,
    );
    expect(
      await screen.findByText(/Provider metadata is unavailable/),
    ).toBeInTheDocument();
    const user = userEvent.setup();
    await user.type(
      screen.getByRole("combobox", {
        name: "Selected providers (custom slugs)",
      }),
      "new-provider{Enter}",
    );
    expect(staged[ORDER]).toEqual(["existing", "new-provider"]);
    await user.type(
      screen.getByRole("combobox", {
        name: "Selected providers (custom slugs)",
      }),
      "bad//slug{Enter}",
    );
    expect(staged[ORDER]).toEqual(["existing", "new-provider"]);
    expect(screen.getByText(/Enter a valid provider slug/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use SmartFast" }));
    expect(staged[ROUTING]).toBe("smartfast");
    expect(staged[ORDER]).toEqual(["existing", "new-provider"]);
  });
});

describe("provider metadata", () => {
  it("retains free prices and rejects missing, invalid and unbounded values", () => {
    const result = parseProviderEndpoints({
      data: {
        endpoints: [
          {
            tag: "free-provider",
            status: 0,
            pricing: { prompt: "0", completion: "0" },
          },
          {
            tag: "missing",
            status: 1,
            pricing: { prompt: "", completion: "Infinity" },
          },
          { tag: "invalid slug", status: 0 },
        ],
      },
    });
    expect(result).toHaveLength(2);
    expect(result[0].inputPrice).toBe(0);
    expect(result[1]).toMatchObject({
      available: false,
      inputPrice: null,
      outputPrice: null,
    });
    expect(priceLabel(null)).toBe("Unknown");
    expect(priceLabel(0)).toBe("Free");
    expect(priceLabel(0.00000025)).toBe("$0.25/M");
    expect(() => parseProviderEndpoints({})).toThrow();
  });
});
