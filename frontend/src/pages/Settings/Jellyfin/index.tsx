import { FunctionComponent, useCallback, useState } from "react";
import {
  Button,
  Group,
  Select,
  Stack,
  Text as MantineText,
} from "@mantine/core";
import {
  useJellyfinRefreshLibrariesMutation,
  useJellyfinTestConnectionMutation,
} from "@/apis/hooks/jellyfin";
import {
  Check,
  CollapseBox,
  Layout,
  Password,
  Section,
  Text,
} from "@/pages/Settings/components";
import { jellyfinEnabledKey } from "@/pages/Settings/keys";
import {
  BaseInput,
  useBaseInput,
  useSettingValue,
} from "@/pages/Settings/utilities/hooks";
import LibrarySelector from "./LibrarySelector";

const JellyfinTestButton: FunctionComponent = () => {
  const [title, setTitle] = useState("Test");
  const [color, setColor] = useState("primary");
  const mutation = useJellyfinTestConnectionMutation();

  const jellyfinUrl = useSettingValue<string>("settings-jellyfin-url");
  const jellyfinApikey = useSettingValue<string>("settings-jellyfin-apikey");

  const click = useCallback(() => {
    if (!jellyfinUrl || !jellyfinApikey) {
      setTitle("URL and API Key required");
      setColor("danger");
      return;
    }

    setTitle("Testing...");
    setColor("primary");

    mutation.mutate(
      { url: jellyfinUrl, apikey: jellyfinApikey },
      {
        onSuccess: (data) => {
          if (data.success) {
            setTitle(`${data.server_name} (v${data.version})`);
            setColor("success");
          } else {
            setTitle(
              data.error_code === "configuration"
                ? "URL and API Key required"
                : "Connection failed",
            );
            setColor("danger");
          }
        },
        onError: () => {
          setTitle("Connection failed");
          setColor("danger");
        },
      },
    );
  }, [jellyfinUrl, jellyfinApikey, mutation]);

  return (
    <Button autoContrast onClick={click} variant={color}>
      {title}
    </Button>
  );
};

const refreshMethodOptions = [
  {
    value: "immediate",
    label: "Immediate",
    description:
      "Re-read item metadata right away without contacting external providers. Recommended for most setups.",
  },
  {
    value: "async",
    label: "Async",
    description:
      "Notify Jellyfin of a filesystem change. Jellyfin picks it up in the background after ~30-60 seconds.",
  },
];

const RefreshMethodSelector: FunctionComponent = () => {
  const { value, update } = useBaseInput<BaseInput<string>, string>({
    settingKey: "settings-jellyfin-refresh_method",
  });

  return (
    <Select
      label="How to notify Jellyfin after subtitle changes"
      data={refreshMethodOptions}
      value={value ?? "immediate"}
      onChange={(v) => update(v)}
      renderOption={({ option }) => {
        const item = refreshMethodOptions.find((o) => o.value === option.value);
        return (
          <Stack gap="xs">
            <MantineText size="sm" fw={500}>
              {item?.label}
            </MantineText>
            <MantineText size="xs" c="dimmed">
              {item?.description}
            </MantineText>
          </Stack>
        );
      }}
    />
  );
};

const SettingsJellyfinView: FunctionComponent = () => {
  return (
    <Layout name="Interface">
      <Section header="Use Jellyfin Media Server">
        <Check label="Enabled" settingKey={jellyfinEnabledKey} />
      </Section>

      <CollapseBox settingKey={jellyfinEnabledKey}>
        <Section header="Connection">
          <Text
            label="Server URL"
            settingKey="settings-jellyfin-url"
            placeholder="http://localhost:8096"
            description="Full URL of your Jellyfin server (e.g., http://192.168.1.100:8096)"
          />
          <Password
            label="API Key"
            settingKey="settings-jellyfin-apikey"
            placeholder="Enter your Jellyfin API key"
            description="Generate an API key in Jellyfin Dashboard > API Keys"
          />
          <RefreshMethodSelector />
          <JellyfinTestButton />
        </Section>

        <Section header="Movie Library">
          <LibrarySelector
            label="Library Name"
            settingKey="settings-jellyfin-movie_library"
            settingKeyIds="settings-jellyfin-movie_library_ids"
            libraryType="movies"
            description="Select your movie library from Jellyfin"
          />
          <Check
            label="Refresh movie metadata after downloading subtitles"
            settingKey="settings-jellyfin-update_movie_library"
          />
        </Section>

        <Section header="Series Library">
          <LibrarySelector
            label="Library Name"
            settingKey="settings-jellyfin-series_library"
            settingKeyIds="settings-jellyfin-series_library_ids"
            libraryType="tvshows"
            description="Select your TV show library from Jellyfin"
          />
          <Check
            label="Refresh series metadata after downloading subtitles"
            settingKey="settings-jellyfin-update_series_library"
          />
        </Section>

        <Section header="Maintenance">
          <JellyfinRefreshNowButton />
        </Section>
      </CollapseBox>
    </Layout>
  );
};

const JellyfinRefreshNowButton: FunctionComponent = () => {
  const [label, setLabel] = useState("Refresh all libraries now");
  const [color, setColor] = useState<string>("light");
  const mutation = useJellyfinRefreshLibrariesMutation();

  const click = useCallback(() => {
    setLabel("Refreshing...");
    setColor("light");
    mutation.mutate(undefined, {
      onSuccess: (data) => {
        const total = data.movies_total + data.series_total;
        const done = data.movies_refreshed + data.series_refreshed;
        if (data.error_code === "configuration") {
          setLabel("Configure URL and API Key first");
          setColor("danger");
          return;
        }
        if (data.error_code === "no_libraries_configured") {
          setLabel("No libraries configured");
          setColor("danger");
          return;
        }
        if (data.error_code === "connection_failed") {
          setLabel("Connection failed");
          setColor("danger");
          return;
        }
        setLabel(`Refreshed ${done} of ${total}`);
        setColor(data.success ? "success" : "danger");
      },
      onError: () => {
        setLabel("Refresh failed");
        setColor("danger");
      },
    });
  }, [mutation]);

  return (
    <Group>
      <Button autoContrast onClick={click} variant={color}>
        {label}
      </Button>
      <MantineText size="xs" c="dimmed">
        Triggers a metadata refresh on every configured Jellyfin library. Useful
        after changing libraries or restarting Jellyfin.
      </MantineText>
    </Group>
  );
};

export default SettingsJellyfinView;
