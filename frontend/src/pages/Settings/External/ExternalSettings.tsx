import { FunctionComponent } from "react";
import {
  Alert,
  Button,
  Group,
  Stack,
  Text as MantineText,
  Title,
} from "@mantine/core";
import {
  faPowerOff,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystem, useSystemStatus } from "@/apis/hooks";
import {
  Check,
  Layout,
  Message,
  Number,
  Password,
  Section,
} from "@/pages/Settings/components";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import TokenField from "./TokenField";

const ENABLED_KEY = "settings-compat_endpoint-enabled";
const CONSENT_KEY = "settings-compat_endpoint-consent";

const RestartBanner: FunctionComponent = () => {
  const { restart, isMutating } = useSystem();
  const status = useSystemStatus();
  const compatActive = status.data?.compat_active ?? false;
  const persistedEnabled = useSettingValue<boolean>(ENABLED_KEY, {
    original: true,
  });
  const needsRestart = Boolean(persistedEnabled) && !compatActive;
  if (!needsRestart) return null;
  return (
    <Alert
      role="alert"
      color="orange"
      variant="filled"
      radius="md"
      mb="md"
      p="lg"
      icon={<FontAwesomeIcon icon={faTriangleExclamation} size="xl" />}
      styles={{
        root: { border: "2px solid var(--mantine-color-orange-7)" },
        icon: { alignSelf: "flex-start", marginTop: 4 },
      }}
    >
      <Stack gap="xs">
        <Title order={4} c="white" style={{ margin: 0 }}>
          Restart required to activate the endpoint
        </Title>
        <MantineText size="sm" c="white">
          You enabled the Subtitle API Endpoint, but it is not running yet. The
          API token is generated during Bazarr startup. Restart now to activate
          the endpoint and reveal the token below.
        </MantineText>
        <Group mt="xs">
          <Button
            size="md"
            color="white"
            c="orange.9"
            variant="white"
            leftSection={<FontAwesomeIcon icon={faPowerOff} />}
            onClick={() => restart()}
            loading={isMutating}
          >
            Restart Bazarr now
          </Button>
        </Group>
      </Stack>
    </Alert>
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
        <Number
          label="Search timeout (seconds)"
          settingKey="settings-compat_endpoint-search_timeout_seconds"
          min={5}
          max={120}
          step={1}
        />
        <Message>
          Hard upper bound for a single fanout across all enabled subtitle
          providers. Providers that exceed this budget are abandoned for the
          current request; three consecutive abandonments temporarily remove
          them from the rotation so remaining providers get more thread time.
          Lower values respond faster but miss slow-but-capable scraper
          providers; higher values return more complete results. Default: 20.
        </Message>
      </Section>
      <Section header="Metadata Providers">
        <MantineText size="sm">
          Optional API keys for enriching searches when a movie or show is not
          in your local library. Used to resolve title and year from an IMDb ID
          so provider matching works on out-of-library content.
        </MantineText>
        <Password
          label="OMDB API Key"
          placeholder="Leave empty to disable OMDB enrichment"
          settingKey="settings-omdb-apikey"
        />
        <Message>
          Get a free key at{" "}
          <a
            href="https://www.omdbapi.com/apikey.aspx"
            target="_blank"
            rel="noreferrer"
          >
            omdbapi.com
          </a>{" "}
          (1000 requests/day). Only used as a fallback for movies not yet in
          your Bazarr library. Episodes are covered by TVDB automatically.
        </Message>
      </Section>
    </Layout>
  );
};

export default SettingsExternalView;
