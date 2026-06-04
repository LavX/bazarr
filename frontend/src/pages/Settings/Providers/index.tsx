import { FunctionComponent, useMemo, useState } from "react";
import {
  Anchor,
  Button,
  Stack,
  Tabs,
  Text as MantineText,
} from "@mantine/core";
import {
  faGears,
  faListCheck,
  faRotate,
  faStore,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useProviderHubCatalog, useProviderHubProviders } from "@/apis/hooks";
import type { ProviderHubInstallation } from "@/apis/raw/providerHub";
import {
  Check,
  CollapseBox,
  Layout,
  Message,
  Password,
  Selector,
  Text,
} from "@/pages/Settings/components";
import {
  ActivityPanel,
  MarketplacePanel,
  MyProvidersPanel,
  UpdateBanner,
  UpdatesPanel,
} from "@/pages/Settings/Providers/hub";
import { summarizeUpdates } from "@/pages/Settings/Providers/hub/utils";
import { antiCaptchaOption } from "@/pages/Settings/Providers/options";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import { ProviderView } from "./components";
import {
  AvailableInput,
  IntegrationList,
  ProviderInfo,
  ProviderList,
} from "./list";

type TabKey = "my-providers" | "marketplace" | "updates" | "activity";

function schemaToInputs(
  manifest: LooseObject | undefined,
): ProviderInfo["inputs"] {
  const schema = manifest?.config_schema;
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
    return undefined;
  }
  const properties = (schema as LooseObject).properties;
  if (
    !properties ||
    typeof properties !== "object" ||
    Array.isArray(properties)
  ) {
    return undefined;
  }
  const secretFields = new Set(
    Array.isArray(manifest?.secret_fields) ? manifest?.secret_fields : [],
  );

  const inputs: AvailableInput[] = [];
  for (const [key, value] of Object.entries(properties)) {
    const field = value as LooseObject;
    if (!field || typeof field !== "object" || Array.isArray(field)) {
      continue;
    }
    const title = typeof field.title === "string" ? field.title : undefined;
    const description =
      typeof field.description === "string" ? field.description : undefined;
    const common = {
      key,
      name: title,
      description,
      defaultValue: field.default as never,
    };

    if (secretFields.has(key) || field.secret === true) {
      inputs.push({ ...common, type: "password" });
      continue;
    }
    if (field.type === "boolean") {
      inputs.push({ ...common, type: "switch" });
      continue;
    }
    if (Array.isArray(field.enum)) {
      inputs.push({
        ...common,
        type: "select",
        options: field.enum.map((item) => ({
          label: String(item),
          value: String(item),
        })),
      });
      continue;
    }
    if (
      field.type === "string" ||
      field.type === "number" ||
      field.type === "integer"
    ) {
      inputs.push({ ...common, type: "text" });
    }
  }

  return inputs.length > 0 ? inputs : undefined;
}

function providerHubOption(provider: ProviderHubInstallation): ProviderInfo {
  const manifest = provider.manifest;
  const manifestLanguages = manifest?.languages;
  const languages = Array.isArray(manifestLanguages)
    ? manifestLanguages.filter(
        (entry): entry is string => typeof entry === "string",
      )
    : undefined;
  return {
    key: provider.provider_id,
    name:
      provider.name ??
      (typeof manifest?.name === "string" ? manifest.name : undefined),
    description:
      (typeof manifest?.description === "string"
        ? manifest.description
        : undefined) ??
      (typeof manifest?.summary === "string" ? manifest.summary : undefined) ??
      "Installed Provider Hub provider.",
    inputs: schemaToInputs(manifest),
    message:
      "Provider Hub plugin is installed but not enabled until you add it here and save settings. Its credentials are stored with the rest of Provider settings.",
    source: "plugin",
    languages,
  };
}

function useProviderOptions(
  providers: ProviderHubInstallation[] | undefined,
): Readonly<ProviderInfo[]> {
  return useMemo(() => {
    const active = (providers ?? []).filter(
      (provider) => provider.state === "active" && !provider.pending_restart,
    );
    // A catalog plugin reuses the built-in's id when it replaces it (e.g. subdl,
    // embeddedsubtitles). Drop the shipped card for any id an active plugin owns
    // so the plugin replaces it instead of appearing as a second duplicate, and
    // so a same-id plugin can actually be added (the shipped key no longer masks
    // it). Plugins with a brand-new id are simply appended.
    const replaced = new Set(active.map((provider) => provider.provider_id));
    const base = ProviderList.filter((provider) => !replaced.has(provider.key));
    const seen = new Set(base.map((provider) => provider.key));
    const hubOptions = active
      .filter((provider) => !seen.has(provider.provider_id))
      .map((provider) => {
        seen.add(provider.provider_id);
        return providerHubOption(provider);
      });
    return [...base, ...hubOptions];
  }, [providers]);
}

const EnabledProvidersSection: FunctionComponent<{
  providerOptions: Readonly<ProviderInfo[]>;
  onBrowseMarketplace: () => void;
}> = ({ providerOptions, onBrowseMarketplace }) => {
  const enabled = useSettingValue<string[]>(
    "settings-general-enabled_providers",
  );
  const isEmpty = Array.isArray(enabled) && enabled.length === 0;
  return (
    <Stack gap="xs">
      <Check
        label="Provider Priority"
        settingKey="settings-general-use_provider_priority"
      />
      <Message>
        Query providers in priority order and stop when a subtitle meeting the
        minimum score is found. When disabled, all providers are queried
        simultaneously and the best result is selected.
      </Message>
      {isEmpty ? (
        <Stack gap="xs" align="flex-start" py="md">
          <MantineText fw={600}>No providers enabled</MantineText>
          <Message>
            Install subtitle providers from the Marketplace, then add them here.
          </Message>
          <Button variant="light" onClick={onBrowseMarketplace}>
            Browse the Marketplace
          </Button>
        </Stack>
      ) : (
        <ProviderView
          addLabel="Add search provider"
          availableOptions={providerOptions}
          settingsKey="settings-general-enabled_providers"
        />
      )}
    </Stack>
  );
};

const AntiCaptchaSection: FunctionComponent = () => (
  <Stack gap="xs">
    <Selector
      clearable
      label={"Choose the anti-captcha provider you want to use"}
      placeholder="Select a provider"
      settingKey="settings-general-anti_captcha_provider"
      options={antiCaptchaOption}
    />
    <CollapseBox
      settingKey="settings-general-anti_captcha_provider"
      on={(value) => value === "anti-captcha"}
    >
      <Text
        label="Account Key"
        settingKey="settings-anticaptcha-anti_captcha_key"
      />
      <Anchor href="http://getcaptchasolution.com/eixxo1rsnw">
        Anti-Captcha.com
      </Anchor>
      <Message>Link to subscribe</Message>
    </CollapseBox>
    <CollapseBox
      settingKey="settings-general-anti_captcha_provider"
      on={(value) => value === "death-by-captcha"}
    >
      <Text label="Username" settingKey="settings-deathbycaptcha-username" />
      <Password
        label="Password"
        settingKey="settings-deathbycaptcha-password"
      />
      <Anchor href="https://www.deathbycaptcha.com">DeathByCaptcha.com</Anchor>
      <Message>Link to subscribe</Message>
    </CollapseBox>
  </Stack>
);

const IntegrationsSection: FunctionComponent = () => (
  <ProviderView
    addLabel="Add integration"
    availableOptions={IntegrationList}
    settingsKey="settings-general-enabled_integrations"
  />
);

const SettingsProvidersView: FunctionComponent = () => {
  const [tab, setTab] = useState<TabKey>("my-providers");
  const catalog = useProviderHubCatalog();
  const providers = useProviderHubProviders();
  const providerOptions = useProviderOptions(providers.data);

  const summary = summarizeUpdates(providers.data, catalog.data);
  const updateBadge = summary.available.length + summary.pendingRestart.length;

  return (
    <Layout name="Subtitle Hub" fluid>
      <UpdateBanner
        providers={providers.data}
        catalog={catalog.data}
        onSwitchToUpdates={() => setTab("updates")}
      />
      <Tabs
        value={tab}
        onChange={(v) => v && setTab(v as TabKey)}
        keepMounted={false}
      >
        <Tabs.List>
          <Tabs.Tab
            value="my-providers"
            leftSection={<FontAwesomeIcon icon={faGears} />}
          >
            My Providers
          </Tabs.Tab>
          <Tabs.Tab
            value="marketplace"
            leftSection={<FontAwesomeIcon icon={faStore} />}
          >
            Marketplace
          </Tabs.Tab>
          <Tabs.Tab
            value="updates"
            leftSection={<FontAwesomeIcon icon={faRotate} />}
            rightSection={
              updateBadge > 0 ? (
                <span
                  aria-label={`${updateBadge} pending update${
                    updateBadge === 1 ? "" : "s"
                  }`}
                  style={{
                    display: "inline-flex",
                    minWidth: 18,
                    height: 18,
                    padding: "0 6px",
                    borderRadius: 9,
                    background: "var(--bz-stat-queued)",
                    color: "white",
                    fontSize: 11,
                    fontWeight: 600,
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  {updateBadge > 9 ? "9+" : updateBadge}
                </span>
              ) : null
            }
          >
            Updates
          </Tabs.Tab>
          <Tabs.Tab
            value="activity"
            leftSection={<FontAwesomeIcon icon={faListCheck} />}
          >
            Activity
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="my-providers" pt="md">
          <MyProvidersPanel
            enabledProviders={
              <EnabledProvidersSection
                providerOptions={providerOptions}
                onBrowseMarketplace={() => setTab("marketplace")}
              />
            }
            antiCaptcha={<AntiCaptchaSection />}
            integrations={<IntegrationsSection />}
          />
        </Tabs.Panel>

        <Tabs.Panel value="marketplace" pt="md">
          <MarketplacePanel catalog={catalog.data} providers={providers.data} />
        </Tabs.Panel>

        <Tabs.Panel value="updates" pt="md">
          <UpdatesPanel providers={providers.data} catalog={catalog.data} />
        </Tabs.Panel>

        <Tabs.Panel value="activity" pt="md">
          <ActivityPanel />
        </Tabs.Panel>
      </Tabs>
    </Layout>
  );
};

export default SettingsProvidersView;
