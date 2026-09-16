import { FC, useState } from "react";
import {
  Alert,
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
import { useMutation } from "@tanstack/react-query";
import { useSettingsMutation } from "@/apis/hooks";
import api from "@/apis/raw";
import type { MediaServerCreate } from "@/apis/raw/mediaServers";
import type { WizardStepProps } from "./types";

/**
 * Optional onboarding step for an external media server. Plex (manual token
 * auth) and Jellyfin each get a tab with the minimal connection fields, saved
 * as the first instance of that kind alongside its master switch. Both are
 * fully optional: only filled tabs are persisted, and Skip advances writing
 * nothing.
 *
 * Plex writes twice on purpose. The instance row is what refreshes subtitles,
 * and the account scalars are what the recently-added dates, the webhook helper
 * and the Autopulse generator still read, so a wizard that wrote only the row
 * would leave those three with no credential.
 */
const MediaServerStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const settings = useSettingsMutation();
  // A destination is a row, not a settings key, so the wizard creates one the
  // same way the Connections page does rather than writing scalars the
  // one-time import has already run past.
  const instance = useMutation({
    mutationFn: (input: MediaServerCreate) => api.mediaServers.create(input),
  });
  const [rejected, setRejected] = useState<string[]>([]);

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

  const handleContinue = async () => {
    const payload: LooseObject = {};
    const rows: MediaServerCreate[] = [];

    if (plexFilled) {
      payload["settings-plex-ip"] = plexIp.trim();
      payload["settings-plex-port"] = Number(plexPort);
      payload["settings-plex-ssl"] = plexSsl;
      payload["settings-plex-apikey"] = plexToken.trim();
      payload["settings-plex-auth_method"] = "apikey";
      payload["settings-general-use_plex"] = true;
      rows.push({
        kind: "plex",
        name: "Plex",
        enabled: true,
        url: `${plexSsl ? "https" : "http"}://${plexIp.trim()}:${Number(plexPort)}`,
        verify_ssl: plexSsl,
        api_key: plexToken.trim(),
      });
    }

    if (jellyfinFilled) {
      payload["settings-general-use_jellyfin"] = true;
      rows.push({
        kind: "jellyfin",
        name: "Jellyfin",
        enabled: true,
        url: jellyfinUrl.trim(),
        verify_ssl: jellyfinVerifySsl,
        api_key: jellyfinApiKey.trim(),
      });
    }

    // A server that refuses one row must not strand the wizard, so the refusal
    // is named here and setup continues; the connection is finished later on
    // the Connections page.
    const refused: string[] = [];
    for (const row of rows) {
      try {
        await instance.mutateAsync(row);
      } catch {
        refused.push(row.name);
      }
    }
    if (Object.keys(payload).length > 0) {
      settings.mutate(payload);
    }
    if (refused.length) {
      setRejected(refused);
      return;
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

      {rejected.length > 0 && (
        <Alert color="red">
          {rejected.join(" and ")} could not be saved. Check the address and the
          credential, or continue and add the connection from Settings,
          Connections.
        </Alert>
      )}

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
        <Button
          onClick={() => (rejected.length ? onNext() : void handleContinue())}
          loading={settings.isPending || instance.isPending}
        >
          {rejected.length
            ? "Continue anyway"
            : anythingFilled
              ? "Continue"
              : "Continue without a server"}
        </Button>
      </Group>
    </Stack>
  );
};

export default MediaServerStep;
