import { FunctionComponent } from "react";
import {
  Alert,
  Button,
  Group,
  Notification,
  Text as MantineText,
} from "@mantine/core";
import { useSystem } from "@/apis/hooks";
import { Check, Layout, Message, Section } from "@/pages/Settings/components";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import TokenField from "./TokenField";

const ENABLED_KEY = "settings-compat_endpoint-enabled";
const CONSENT_KEY = "settings-compat_endpoint-consent";

const RestartBanner: FunctionComponent = () => {
  const { restart, isMutating } = useSystem();
  const persistedEnabled = useSettingValue<boolean>(ENABLED_KEY, {
    original: true,
  });
  const persistedToken = useSettingValue<string>(
    "settings-compat_endpoint-token",
    { original: true },
  );
  // Only show the banner AFTER a successful save has landed with enabled=true
  // but the running Bazarr process still has an empty token (ensure_secrets
  // populates it at the next boot). Showing the banner only in this gap means:
  // - never before the user has saved
  // - never while the endpoint is off
  // - always when a restart is actually required to activate the endpoint
  const needsRestart = Boolean(persistedEnabled) && !persistedToken;
  if (!needsRestart) return null;
  return (
    <Notification
      color="blue"
      title="Restart required"
      role="alert"
      withCloseButton={false}
      mb="md"
    >
      <Group justify="space-between" align="center" wrap="nowrap">
        <MantineText size="sm">
          Save this page, then restart Bazarr to apply changes to the Subtitle
          API Endpoint.
        </MantineText>
        <Button
          size="xs"
          variant="light"
          color="blue"
          onClick={() => restart()}
          disabled={isMutating}
        >
          Restart Bazarr
        </Button>
      </Group>
    </Notification>
  );
};

const TokenSection: FunctionComponent = () => {
  // Token exists on disk only after the first save with enabled=true (the
  // server auto-generates it in ensure_secrets). Don't show the field until
  // the persisted value is non-empty; otherwise the user sees an empty input
  // that looks broken.
  const persistedEnabled = useSettingValue<boolean>(ENABLED_KEY, {
    original: true,
  });
  const persistedToken = useSettingValue<string>(
    "settings-compat_endpoint-token",
    {
      original: true,
    },
  );
  if (!persistedEnabled || !persistedToken) {
    return (
      <Message>
        The API token will appear here after you tick <b>Enable</b>, save this
        page, and restart Bazarr. Until then, there is no token to share with
        clients.
      </Message>
    );
  }
  return <TokenField />;
};

const SettingsExternalView: FunctionComponent = () => {
  return (
    <Layout name="External Integration">
      <Section header="Subtitle API Endpoint">
        <MantineText size="sm">
          Expose a REST API so external subtitle clients can search and download
          subtitles through your configured providers. Compatible with common
          VLC, Kodi, Jellyfin, and media-center subtitle plugins.
        </MantineText>
        <RestartBanner />
        <Alert color="yellow" mt="xs" mb="xs">
          Do not expose this endpoint to the public internet. You are
          responsible for provider ToS compliance. Some providers
          (OpenSubtitles.com, SubDL, and others) prohibit proxying their service
          or sharing API keys with third-party clients.
        </Alert>
        <Check
          label="I understand this endpoint must not be exposed to the public internet and I am responsible for provider ToS compliance."
          settingKey={CONSENT_KEY}
        />
        <Check label="Enable" settingKey={ENABLED_KEY} />
        <TokenSection />
        <Message>
          This endpoint implements a REST API shape used by OpenSubtitles.com
          for interoperability with existing OpenSubtitles-compatible
          media-center plugins. Bazarr+ is not affiliated with or endorsed by
          OpenSubtitles.com.
        </Message>
      </Section>
    </Layout>
  );
};

export default SettingsExternalView;
