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
import { useWizardStep } from "@/pages/Setup/useWizardStep";
import type { WizardStepProps } from "./types";

interface SummaryLine {
  label: string;
  done: boolean;
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
 * Final onboarding step. Summarizes what the wizard configured from live state,
 * then Finish marks setup complete and routes the user into the app. Mirrors the
 * shell's handleSkip (mutate setup_complete, then reset + navigate on success).
 */
const FinishStep: FC<WizardStepProps> = ({ onBack }) => {
  const navigate = useNavigate();
  const { reset } = useWizardStep();
  const mutation = useSettingsMutation();

  const { data: instances } = useArrInstances();
  const { data: profiles } = useLanguageProfiles();
  const { data: settings } = useSystemSettings();

  const general = settings?.general;

  const sonarrCount = (instances ?? []).filter(
    (i) => i.kind === "sonarr",
  ).length;
  const radarrCount = (instances ?? []).filter(
    (i) => i.kind === "radarr",
  ).length;
  const profileCount = (profiles ?? []).length;
  const providers = general?.enabled_providers ?? [];
  const usePlex = general?.use_plex ?? false;
  const useJellyfin = general?.use_jellyfin ?? false;

  const lines: SummaryLine[] = [
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
  ];

  const handleFinish = () => {
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          reset();
          // The Redirector picks routing back up once setup is marked complete.
          navigate("/");
        },
      },
    );
  };

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>You are all set</Title>
        <Text c="dimmed">
          Here is a recap of what we configured. You can revisit any of this in
          Settings whenever you like.
        </Text>
      </Stack>

      <List spacing="sm" center>
        {lines.map((line) => (
          <SummaryItem key={line.label} label={line.label} done={line.done} />
        ))}
      </List>

      <Group justify="space-between">
        <Group gap="sm">
          {onBack && (
            <Button variant="default" onClick={onBack}>
              Back
            </Button>
          )}
        </Group>
        <Button onClick={handleFinish} loading={mutation.isPending} size="md">
          Finish
        </Button>
      </Group>
    </Stack>
  );
};

export default FinishStep;
