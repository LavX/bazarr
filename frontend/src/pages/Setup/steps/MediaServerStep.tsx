import { FC, useState } from "react";
import {
  Button,
  Group,
  NumberInput,
  PasswordInput,
  Stack,
  Switch,
  Tabs,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import type { WizardStepProps } from "./types";

/**
 * Optional onboarding step for an external media server. Plex (manual apikey
 * auth) and Jellyfin each get a tab with the minimal connection fields, written
 * straight to the matching settings keys. Both are fully optional: only filled
 * tabs are persisted, and Skip advances writing nothing.
 */
const MediaServerStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const settings = useSettingsMutation();

  // Plex (manual apikey auth) connection fields.
  const [plexIp, setPlexIp] = useState("");
  const [plexPort, setPlexPort] = useState<number | string>(32400);
  const [plexSsl, setPlexSsl] = useState(false);
  const [plexToken, setPlexToken] = useState("");

  // Jellyfin connection fields.
  const [jellyfinUrl, setJellyfinUrl] = useState("");
  const [jellyfinApiKey, setJellyfinApiKey] = useState("");
  const [jellyfinVerifySsl, setJellyfinVerifySsl] = useState(true);

  const plexFilled = plexIp.trim().length > 0 || plexToken.trim().length > 0;
  const jellyfinFilled =
    jellyfinUrl.trim().length > 0 || jellyfinApiKey.trim().length > 0;

  const handleContinue = () => {
    const payload: LooseObject = {};

    if (plexFilled) {
      payload["settings-plex-ip"] = plexIp.trim();
      payload["settings-plex-port"] = Number(plexPort);
      payload["settings-plex-ssl"] = plexSsl;
      payload["settings-plex-apikey"] = plexToken.trim();
      payload["settings-plex-auth_method"] = "apikey";
      payload["settings-general-use_plex"] = true;
    }

    if (jellyfinFilled) {
      payload["settings-jellyfin-url"] = jellyfinUrl.trim();
      payload["settings-jellyfin-apikey"] = jellyfinApiKey.trim();
      payload["settings-jellyfin-verify_ssl"] = jellyfinVerifySsl;
      payload["settings-general-use_jellyfin"] = true;
    }

    if (Object.keys(payload).length > 0) {
      settings.mutate(payload);
    }
    onNext();
  };

  const anythingFilled = plexFilled || jellyfinFilled;

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>Media servers</Title>
        <Text c="dimmed">
          Optionally connect Plex or Jellyfin so Bazarr can refresh metadata
          after it downloads subtitles. You can skip this and add it later.
        </Text>
      </Stack>

      <Tabs defaultValue="plex">
        <Tabs.List>
          <Tabs.Tab value="plex">Plex</Tabs.Tab>
          <Tabs.Tab value="jellyfin">Jellyfin</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="plex" pt="md">
          <Stack gap="md">
            <Group gap="md" align="flex-start" wrap="nowrap">
              <TextInput
                label="Address"
                description="Hostname or IPv4 address"
                placeholder="127.0.0.1"
                style={{ flex: 1 }}
                value={plexIp}
                onChange={(e) => setPlexIp(e.currentTarget.value)}
              />
              <NumberInput
                label="Port"
                w={110}
                min={1}
                max={65535}
                allowDecimal={false}
                hideControls
                value={plexPort}
                onChange={setPlexPort}
              />
            </Group>
            <Switch
              label="Use SSL"
              checked={plexSsl}
              onChange={(e) => setPlexSsl(e.currentTarget.checked)}
            />
            <PasswordInput
              label="Token"
              description="Your Plex authentication token (X-Plex-Token)"
              placeholder="Plex token"
              autoComplete="new-password"
              value={plexToken}
              onChange={(e) => setPlexToken(e.currentTarget.value)}
            />
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="jellyfin" pt="md">
          <Stack gap="md">
            <TextInput
              label="Server URL"
              description="Full URL of your Jellyfin server"
              placeholder="http://localhost:8096"
              value={jellyfinUrl}
              onChange={(e) => setJellyfinUrl(e.currentTarget.value)}
            />
            <PasswordInput
              label="API Key"
              description="Generate one in Jellyfin Dashboard, API Keys"
              placeholder="Jellyfin API key"
              autoComplete="new-password"
              value={jellyfinApiKey}
              onChange={(e) => setJellyfinApiKey(e.currentTarget.value)}
            />
            <Switch
              label="Verify SSL certificate"
              checked={jellyfinVerifySsl}
              onChange={(e) => setJellyfinVerifySsl(e.currentTarget.checked)}
            />
          </Stack>
        </Tabs.Panel>
      </Tabs>

      <Group justify="space-between">
        <Group gap="sm">
          {onBack && (
            <Button variant="default" onClick={onBack}>
              Back
            </Button>
          )}
          <Button variant="subtle" color="gray" onClick={onNext}>
            Skip
          </Button>
        </Group>
        <Button onClick={handleContinue} loading={settings.isPending}>
          {anythingFilled ? "Continue" : "Continue without a server"}
        </Button>
      </Group>
    </Stack>
  );
};

export default MediaServerStep;
