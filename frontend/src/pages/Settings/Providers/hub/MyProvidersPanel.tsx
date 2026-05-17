import { FunctionComponent, ReactNode } from "react";
import { Stack } from "@mantine/core";
import {
  useProviderHubTest,
  useProviderHubUninstall,
} from "@/apis/hooks/providerHub";
import type { ProviderHubInstallation } from "@/apis/raw/providerHub";
import { ProviderCard } from "@/pages/Settings/Providers/hub/components/ProviderCard";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface MyProvidersPanelProps {
  enabledProviders: ReactNode;
  installedPlugins?: ProviderHubInstallation[];
  antiCaptcha: ReactNode;
  integrations: ReactNode;
}

interface SectionHeaderProps {
  eyebrow: string;
  title: string;
  description?: string;
}

const SectionHeader: FunctionComponent<SectionHeaderProps> = ({
  eyebrow,
  title,
  description,
}) => (
  <div>
    <div className={styles.eyebrow}>{eyebrow}</div>
    <h3 className={styles.panelTitle} style={{ fontSize: 18 }}>
      {title}
    </h3>
    {description && <p className={styles.panelDescription}>{description}</p>}
  </div>
);

export const MyProvidersPanel: FunctionComponent<MyProvidersPanelProps> = ({
  enabledProviders,
  installedPlugins = [],
  antiCaptcha,
  integrations,
}) => {
  const testProvider = useProviderHubTest();
  const uninstall = useProviderHubUninstall();

  return (
    <Stack gap="xl">
      <Stack gap="md">
        <SectionHeader
          eyebrow="Subtitle providers"
          title="Enabled search providers"
          description="Add shipped providers or active Provider Hub plugins from the same plus button. Installed plugins do not search until you add them here and save settings."
        />
        {enabledProviders}
      </Stack>

      {installedPlugins.length > 0 && (
        <Stack gap="md">
          <SectionHeader
            eyebrow="Provider Hub"
            title="Installed plugins"
            description="Manage installed Provider Hub plugins here. Active plugins are searchable only after you add them to Enabled search providers and save settings."
          />
          <div className={styles.cardGrid}>
            {installedPlugins.map((provider) => (
              <ProviderCard
                key={provider.provider_id}
                provider={provider}
                onTest={(providerId) => testProvider.mutate(providerId)}
                onUninstall={(providerId) => uninstall.mutate(providerId)}
                isTesting={
                  testProvider.isPending &&
                  testProvider.variables === provider.provider_id
                }
              />
            ))}
          </div>
        </Stack>
      )}

      <Stack gap="md">
        <SectionHeader
          eyebrow="Captcha solving"
          title="Anti-captcha"
          description="Required for web-scraper providers (OpenSubtitles.org, Addic7ed, etc.) that gate downloads behind a captcha challenge."
        />
        {antiCaptcha}
      </Stack>

      <Stack gap="md">
        <SectionHeader
          eyebrow="Integrations"
          title="Embedded subtitles and metadata"
          description="Special-purpose providers that extract subtitles from media files or supplement metadata. Configure like any other provider."
        />
        {integrations}
      </Stack>
    </Stack>
  );
};
