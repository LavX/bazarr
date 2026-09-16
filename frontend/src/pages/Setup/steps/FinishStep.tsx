import { FC } from "react";
import { useNavigate } from "react-router";
import {
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
import {
  clearPersistedIntent,
  useOnboardingIntent,
} from "@/pages/Setup/useOnboardingIntent";
import { useWizardStep } from "@/pages/Setup/useWizardStep";
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
  const { reset } = useWizardStep();
  const { intent } = useOnboardingIntent();
  const mutation = useSettingsMutation();

  const { data: instances } = useArrInstances();
  const { data: profiles } = useLanguageProfiles();
  const { data: settings } = useSystemSettings();

  const general = settings?.general;
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
  const usePlex = general?.use_plex ?? false;
  const useJellyfin = general?.use_jellyfin ?? false;
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
    {
      label: usePlex
        ? "Plex media server connected"
        : "Plex media server skipped",
      done: usePlex,
    },
    {
      label: useJellyfin
        ? "Jellyfin media server connected"
        : "Jellyfin media server skipped",
      done: useJellyfin,
    },
    {
      label: useSeerr ? "Seerr connected" : "Seerr skipped",
      done: useSeerr,
    },
  ];

  const sharedLines: SummaryLine[] = [
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

  const lines = discoverPath ? sharedLines : [...libraryLines, ...sharedLines];

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
  if (!discoverPath && !usePlex && !useJellyfin) {
    skipped.push({
      label: "No Plex or Jellyfin media server is connected",
      where: "Settings, Connections",
    });
  }
  if (!discoverPath && !useSeerr) {
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
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          reset();
          clearPersistedIntent();
          // The Redirector picks routing back up once setup is marked
          // complete. A Discover user goes straight to the page they came for.
          navigate(discoverPath ? "/discover" : "/");
        },
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
