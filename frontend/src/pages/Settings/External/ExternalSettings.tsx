import { FunctionComponent, useMemo } from "react";
import { Alert, Notification, Text as MantineText } from "@mantine/core";
import { Check, Layout, Message, Section } from "@/pages/Settings/components";
import { useStagedValues } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import TokenField from "./TokenField";

const SettingsExternalView: FunctionComponent = () => {
  const stagedValues = useStagedValues();
  const originalEnabled = useSettingValue<boolean>("settings-compat-enabled", {
    original: true,
  });

  const enabledIsDirty = useMemo(() => {
    return "settings-compat-enabled" in stagedValues;
  }, [stagedValues]);

  return (
    <Layout name="External Integration">
      <Section header="Subtitle API Endpoint">
        <MantineText size="sm">
          Expose a REST API so external subtitle clients can search and download
          subtitles through your configured providers. Compatible with common
          VLC, Kodi, Jellyfin, and media-center subtitle plugins.
        </MantineText>
        {enabledIsDirty && (
          <Notification
            color="blue"
            title="Restart required"
            role="alert"
            withCloseButton={false}
            mb="md"
          >
            Save and restart Bazarr to apply changes to the Subtitle API
            Endpoint.
          </Notification>
        )}
        <Alert color="yellow" mt="xs" mb="xs">
          Do not expose this endpoint to the public internet. You are
          responsible for provider ToS compliance. Some providers
          (OpenSubtitles.com, SubDL, and others) prohibit proxying their service
          or sharing API keys with third-party clients.
        </Alert>
        <Check
          label="I understand this endpoint must not be exposed to the public internet and I am responsible for provider ToS compliance."
          settingKey="settings-compat-consent"
        />
        <Check label="Enable" settingKey="settings-compat-enabled" />
        <TokenField />
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
