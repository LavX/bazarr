import { FC, useState } from "react";
import { useNavigate } from "react-router";
import {
  Alert,
  Button,
  Group,
  List,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from "@mantine/core";
import { faCheck, faMinus } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useArrInstances,
  useLanguageProfiles,
  useSettingsMutation,
  useSystemSettings,
} from "@/apis/hooks";
import { useMediaServerInstances } from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { kindName } from "@/pages/Settings/MediaServers/kinds";
import {
  clearPersistedIntent,
  useOnboardingIntent,
} from "@/pages/Setup/useOnboardingIntent";
import { clearPersistedSelection } from "@/pages/Setup/useOnboardingSelection";
import { clearPersistedStep } from "@/pages/Setup/useWizardStep";
import type { WizardStepProps } from "./types";

interface SummaryLine {
  label: string;
  done: boolean;
}

interface SkippedLine {
  label: string;
  where: string;
}

function SummaryItem({ label, done }: SummaryLine) {
  return (
    <List.Item
      icon={
        <ThemeIcon color={done ? "green" : "gray"} size={20} radius="xl">
          <FontAwesomeIcon icon={done ? faCheck : faMinus} size="xs" />
        </ThemeIcon>
      }
    >
      <Text c={done ? undefined : "dimmed"}>{label}</Text>
    </List.Item>
  );
}

/**
 * Final onboarding step. Finish marks setup complete and routes the user into
 * the app: a Discover-path user lands on /discover, the page they installed
 * Bazarr+ for, and a library user lands on the homepage with their first scan
 * already running.
 *
 * The recap and the "what you skipped" list are derived from live settings,
 * arr instances and language profiles, which is exactly the state
 * discover/summary.py reads to decide whether an install is a first run. Both
 * views therefore answer from the same facts and cannot disagree about what was
 * set up.
 */
const FinishStep: FC<WizardStepProps> = ({ onBack }) => {
  const navigate = useNavigate();
  const { intent } = useOnboardingIntent();
  const mutation = useSettingsMutation();
  const [failed, setFailed] = useState(false);

  const { data: instances } = useArrInstances();
  const { data: profiles } = useLanguageProfiles();
  const { data: settings } = useSystemSettings();

  // The recap reads the rows, not the master switches. It used to read
  // use_plex and use_jellyfin only, so connecting Emby produced "Plex skipped,
  // Jellyfin skipped" and no mention of the server that was actually set up.
  const plex = useMediaServerInstances("plex");
  const jellyfin = useMediaServerInstances("jellyfin");
  const emby = useMediaServerInstances("emby");
  const silo = useMediaServerInstances("silo");

  const general = settings?.general;

  // A switched-off row is not a destination: the dispatcher skips it, and the
  // backend keeps exactly one after a Plex sign-out, credential cleared.
  // Counting it told the reader Plex was connected and dropped the line saying
  // where to finish the job.
  const activeCount = (rows: MediaServerInstance[] | undefined) =>
    (rows ?? []).filter((row) => row.enabled).length;

  const mediaServers: { kind: MediaServerKind; count: number }[] = [
    { kind: "plex", count: activeCount(plex.data) },
    { kind: "jellyfin", count: activeCount(jellyfin.data) },
    { kind: "emby", count: activeCount(emby.data) },
    { kind: "silo", count: activeCount(silo.data) },
  ];
  const mediaServerCount = mediaServers.reduce(
    (total, entry) => total + entry.count,
    0,
  );
  const discoverPath = intent === "discover";

  const sonarrCount = (instances ?? []).filter(
    (i) => i.kind === "sonarr",
  ).length;
  const radarrCount = (instances ?? []).filter(
    (i) => i.kind === "radarr",
  ).length;
  const sportarrCount = (instances ?? []).filter(
    (i) => i.kind === "sportarr",
  ).length;
  const instanceCount = sonarrCount + radarrCount + sportarrCount;
  const profileCount = (profiles ?? []).length;
  const providers = general?.enabled_providers ?? [];
  const useSeerr = general?.use_seerr ?? false;
  const translatorReady =
    (settings?.translator?.openrouter_api_key ?? "").length > 0;

  const libraryLines: SummaryLine[] = [
    {
      label: sonarrCount
        ? `Sonarr connected (${sonarrCount} ${
            sonarrCount === 1 ? "instance" : "instances"
          })`
        : "Sonarr not connected",
      done: sonarrCount > 0,
    },
    {
      label: radarrCount
        ? `Radarr connected (${radarrCount} ${
            radarrCount === 1 ? "instance" : "instances"
          })`
        : "Radarr not connected",
      done: radarrCount > 0,
    },
    {
      label: sportarrCount
        ? `Sportarr connected (${sportarrCount} ${
            sportarrCount === 1 ? "instance" : "instances"
          })`
        : "Sportarr not connected",
      done: sportarrCount > 0,
    },
  ];

  // Media servers are their own group rather than part of the arr recap: the
  // picker is on both paths, so a Discover reader who connected Jellyfin has to
  // be told about it too. It used to sit in libraryLines, which a Discover run
  // never renders, so that reader finished with no mention of the server they
  // had just set up.
  const mediaServerLines: SummaryLine[] =
    mediaServerCount === 0
      ? [{ label: "No media server connected", done: false }]
      : mediaServers
          .filter((entry) => entry.count > 0)
          .map((entry) => ({
            label: `${kindName(entry.kind)} connected (${entry.count} ${
              entry.count === 1 ? "server" : "servers"
            })`,
            done: true,
          }));

  const sharedLines: SummaryLine[] = [
    {
      label: useSeerr ? "Seerr connected" : "Seerr skipped",
      done: useSeerr,
    },
    {
      label: profileCount
        ? `Language profile created (${profileCount})`
        : "No language profile yet",
      done: profileCount > 0,
    },
    {
      label: providers.length
        ? `${providers.length} subtitle ${
            providers.length === 1 ? "provider" : "providers"
          } enabled`
        : "No subtitle providers enabled yet",
      done: providers.length > 0,
    },
    {
      label: translatorReady
        ? "AI translation configured"
        : "AI translation not configured",
      done: translatorReady,
    },
  ];

  const lines = discoverPath
    ? [...mediaServerLines, ...sharedLines]
    : [...libraryLines, ...mediaServerLines, ...sharedLines];

  // What was left undone, and the page that finishes it. Named rather than
  // linked: setup is not marked complete until Finish is pressed, so a link
  // followed from here would be bounced straight back to the wizard. Only
  // things the user actually walked past appear, so a Discover user is never
  // told they skipped a step nobody showed them.
  const skipped: SkippedLine[] = [];
  if (!discoverPath && instanceCount === 0) {
    skipped.push({
      label: "No Sonarr, Radarr or Sportarr instance is connected",
      where: "Settings, Connections",
    });
  }
  // Not gated on the path, for the same reason the recap is not: both paths
  // are offered the picker now.
  if (mediaServerCount === 0) {
    skipped.push({
      label:
        "No media server is connected, so nothing is refreshed after a download",
      where: "Settings, Connections",
    });
  }
  if (!useSeerr) {
    skipped.push({
      label: "Seerr is not connected, so requests are unavailable",
      where: "Settings, Connections",
    });
  }
  if (!translatorReady) {
    skipped.push({
      label: "AI translation has no API key yet",
      where: "Settings, Translator",
    });
  }

  const handleFinish = () => {
    setFailed(false);
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          clearPersistedStep();
          clearPersistedIntent();
          clearPersistedSelection();
          // The Redirector picks routing back up once setup is marked
          // complete. A Discover user goes straight to the page they came for.
          navigate(discoverPath ? "/discover" : "/");
        },
        // Without this the button stopped spinning and nothing else happened:
        // setup stayed incomplete, so the next load came straight back here
        // with no explanation.
        onError: () => setFailed(true),
      },
    );
  };

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>You are all set</Title>
        <Text c="dimmed">
          {discoverPath
            ? "Search for any film or series, preview what is available and download it straight to this device. No library required."
            : instanceCount > 0
              ? "Your first library scan starts now, and Bazarr+ will begin looking for the subtitles it is missing."
              : "Nothing is connected yet, so Bazarr+ has no library to scan. Discover still works, and you can connect an instance whenever you like."}
        </Text>
      </Stack>

      <List spacing="sm" center>
        {lines.map((line) => (
          <SummaryItem key={line.label} label={line.label} done={line.done} />
        ))}
      </List>

      {skipped.length > 0 && (
        <Stack gap="xs">
          <Text fw={600}>What you left for later</Text>
          <List spacing="xs" size="sm">
            {skipped.map((item) => (
              <List.Item key={item.label}>
                <Text size="sm" c="dimmed">
                  {item.label}. You will find it under {item.where}.
                </Text>
              </List.Item>
            ))}
          </List>
        </Stack>
      )}

      {failed && (
        <Alert color="red" title="Could not finish setup">
          Bazarr+ could not save that setup is complete, so this screen comes
          back on the next load. Check that Bazarr+ is still running, then try
          again.
        </Alert>
      )}

      <Group justify="space-between">
        <Group gap="sm">
          {onBack && (
            <Button variant="default" onClick={onBack}>
              Back
            </Button>
          )}
        </Group>
        <Button onClick={handleFinish} loading={mutation.isPending} size="md">
          {discoverPath ? "Finish and open Discover" : "Finish"}
        </Button>
      </Group>
    </Stack>
  );
};

export default FinishStep;
