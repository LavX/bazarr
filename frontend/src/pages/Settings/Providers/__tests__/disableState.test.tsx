/* eslint-disable camelcase -- Settings fixtures use API field names. */
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ProviderView } from "@/pages/Settings/Providers/components";
import type { ProviderInfo } from "@/pages/Settings/Providers/list";
import { FormContext, FormValues } from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { customRender, screen, within } from "@/tests";

const tuning = {
  "settings-general-provider_priorities": JSON.stringify({
    example: 20,
    other: 10,
  }),
  "settings-general-provider_languages": JSON.stringify({
    example: ["en"],
    other: ["fr"],
  }),
  "settings-general-provider_score_modifiers": JSON.stringify({
    example: 25,
    other: -5,
  }),
  "settings-example-region": "eu",
};

function Harness({
  enabled = ["example", "other"],
  disabled = ["paused"],
  staged = tuning,
  source,
}: {
  enabled?: string[];
  disabled?: string[];
  staged?: LooseObject;
  source?: ProviderInfo["source"];
}) {
  const form = useForm<FormValues>({
    initialValues: { settings: staged, hooks: {} },
  });
  const settings = {
    general: {
      enabled_providers: enabled,
      disabled_providers: disabled,
      provider_priorities: JSON.stringify({ example: 80 }),
      provider_languages: JSON.stringify({ example: ["de"] }),
      provider_score_modifiers: JSON.stringify({ example: 5 }),
    },
    example: { region: "us" },
  } as unknown as Settings;
  const options: ProviderInfo[] = [
    {
      key: "example",
      name: "Example Provider",
      source,
      inputs: [{ key: "region", name: "Region", type: "text" }],
    },
    { key: "other", name: "Other Provider" },
    { key: "paused", name: "Paused Provider" },
  ];

  return (
    <SettingsProvider value={settings}>
      <FormContext.Provider value={form}>
        <ProviderView
          availableOptions={options}
          settingsKey="settings-general-enabled_providers"
        />
        <output aria-label="Staged settings">
          {JSON.stringify(form.values.settings)}
        </output>
      </FormContext.Provider>
    </SettingsProvider>
  );
}

function stagedSettings(): LooseObject {
  return JSON.parse(
    screen.getByRole("status", { name: "Staged settings" }).textContent ?? "{}",
  );
}

async function openProvider(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getByRole("button", { name: /Example Provider \(priority/ }),
  );
  await screen.findByLabelText("Region");
}

describe.each([undefined, "plugin"] as const)(
  "provider disable state (%s source)",
  (source) => {
    it("disables and re-enables in one staged session without changing tuning or configuration", async () => {
      const user = userEvent.setup();
      customRender(<Harness source={source} />);
      await openProvider(user);
      expect(
        screen.getByText(/Disable keeps.*Remove deletes/i),
      ).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Disable" }));

      expect(stagedSettings()).toEqual({
        ...tuning,
        "settings-general-enabled_providers": ["other"],
        "settings-general-disabled_providers": ["paused", "example"],
      });
      expect(
        within(
          screen.getByRole("button", {
            name: /Example Provider \(priority 20\)/,
          }),
        ).getByText("Disabled"),
      ).toBeInTheDocument();

      await openProvider(user);
      await user.click(screen.getByRole("button", { name: "Enable" }));
      expect(stagedSettings()).toEqual({
        ...tuning,
        "settings-general-enabled_providers": ["other", "example"],
        "settings-general-disabled_providers": ["paused"],
      });
      expect(
        within(
          screen.getByRole("button", { name: /Example Provider \(priority/ }),
        ).queryByText("Disabled"),
      ).not.toBeInTheDocument();
    });
  },
);

it("enables a saved disabled provider while keeping its saved tuning", async () => {
  const user = userEvent.setup();
  customRender(
    <Harness enabled={["other"]} disabled={["example"]} staged={{}} />,
  );
  await openProvider(user);
  await user.click(screen.getByRole("button", { name: "Enable" }));
  expect(stagedSettings()).toEqual({
    "settings-general-enabled_providers": ["other", "example"],
    "settings-general-disabled_providers": [],
  });
  expect(
    screen.getByRole("button", { name: /Example Provider \(priority 80\)/ }),
  ).toBeInTheDocument();
});

it.each([
  { enabled: ["example", "other"], disabled: ["paused"] },
  { enabled: ["other"], disabled: ["paused", "example"] },
  { enabled: ["example", "other"], disabled: ["paused", "example"] },
])("removes a provider and its tuning from both lists: %j", async (lists) => {
  const user = userEvent.setup();
  customRender(<Harness {...lists} />);
  await openProvider(user);
  await user.click(screen.getByRole("button", { name: "Remove" }));
  expect(stagedSettings()).toEqual({
    "settings-general-enabled_providers": ["other"],
    "settings-general-disabled_providers": ["paused"],
    "settings-general-provider_priorities": JSON.stringify({ other: 10 }),
    "settings-general-provider_languages": JSON.stringify({ other: ["fr"] }),
    "settings-general-provider_score_modifiers": JSON.stringify({ other: -5 }),
    "settings-example-region": "eu",
  });
  expect(
    screen.queryByRole("button", { name: /Example Provider \(priority/ }),
  ).not.toBeInTheDocument();
});

it("keeps a disabled provider disabled when its configuration is saved", async () => {
  const user = userEvent.setup();
  customRender(<Harness enabled={["other"]} disabled={["example"]} />);
  await openProvider(user);
  await user.clear(screen.getByLabelText("Region"));
  await user.type(screen.getByLabelText("Region"), "uk");
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(stagedSettings()).toEqual({
    ...tuning,
    "settings-example-region": "uk",
  });
  expect(
    within(
      screen.getByRole("button", { name: /Example Provider \(priority 20\)/ }),
    ).getByText("Disabled"),
  ).toBeInTheDocument();
});

it("clears a disabled id when it is added through the existing picker", async () => {
  const user = userEvent.setup();
  customRender(<Harness enabled={["other"]} disabled={["example"]} />);
  await user.click(screen.getByRole("button", { name: "Add provider" }));
  const drawer = await screen.findByRole("dialog");
  await user.click(
    within(drawer).getByRole("button", { name: /Example Provider/ }),
  );
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(stagedSettings()["settings-general-enabled_providers"]).toEqual([
    "other",
    "example",
  ]);
  expect(stagedSettings()["settings-general-disabled_providers"]).toEqual([]);
});
