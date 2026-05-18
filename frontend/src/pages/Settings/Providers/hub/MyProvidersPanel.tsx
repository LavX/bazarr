import { FunctionComponent, ReactNode } from "react";
import { Stack } from "@mantine/core";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface MyProvidersPanelProps {
  enabledProviders: ReactNode;
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
  antiCaptcha,
  integrations,
}) => {
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
