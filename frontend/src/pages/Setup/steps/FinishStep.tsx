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
} from "@mantine/core";
import { faCheck, faMinus } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQueryClient } from "@tanstack/react-query";
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
  clearPersistedConnectionTests,
  ConnectionTest,
  connectionTestKey,
  readConnectionTests,
} from "@/pages/Setup/connectionTests";
import { settleSetupComplete } from "@/pages/Setup/setupCompleteCache";
import StepLayout from "@/pages/Setup/StepLayout";
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
 * The recap line for rows of one kind that exist. "Connected" is kept for rows
 * a Test passed against in this run of the wizard: a row that saved proves only
 * that the values were written, and a wrong address or key saves too.
 */
function savedLine(
  name: string,
  counted: string,
  results: ConnectionTest[],
): SummaryLine {
  if (results.length > 0 && results.every((result) => result === "passed")) {
    return { label: `${name} connected (${counted})`, done: true };
  }
  if (results.includes("failed")) {
    return {
      label: `${name} saved (${counted}), but the connection test failed`,
      done: false,
    };
  }
  return {
    label: `${name} saved (${counted}), connection not tested`,
    done: false,
  };
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
  const client = useQueryClient();
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
  //
  // The kind's master switch counts for the same reason. The dispatcher reads
  // use_<kind> before it reads any row, so a server saved on a step whose
  // switch write failed, which is exactly what "Continue anyway" walks past,
  // refreshes nothing however enabled its own row is. Reporting it connected
  // was the one place left that could still tell the reader setup was done
  // when it was not.
  const activeRows = (
    kind: MediaServerKind,
    rows: MediaServerInstance[] | undefined,
  ) => {
    const flag = general?.[`use_${kind}`] as boolean | undefined;
    // An unanswered settings query is not a switched-off kind. It resolves
    // before this screen is actionable, and assuming off would flash the
    // "nothing is connected" warning at a reader who connected four servers.
    if (general !== undefined && flag !== true) {
      return [];
    }
    return (rows ?? []).filter((row) => row.enabled);
  };

  const mediaServers: {
    kind: MediaServerKind;
    count: number;
    ids: string[];
  }[] = (
    [
      ["plex", plex.data],
      ["jellyfin", jellyfin.data],
      ["emby", emby.data],
      ["silo", silo.data],
    ] as const
  ).map(([kind, data]) => {
    const rows = activeRows(kind, data);
    return { kind, count: rows.length, ids: rows.map((row) => row.id) };
  });
  const mediaServerCount = mediaServers.reduce(
    (total, entry) => total + entry.count,
    0,
  );
  const discoverPath = intent === "discover";

  // What the Test said about each row a connection step saved in this run.
  const tests = readConnectionTests();
  const testsFor = (keys: string[]) =>
    keys.map((key) => tests[key] ?? "untested");

  const arrLine = (kind: "sonarr" | "radarr" | "sportarr", name: string) => {
    const rows = (instances ?? []).filter((i) => i.kind === kind);
    if (rows.length === 0) {
      return {
        count: 0,
        line: { label: `${name} not connected`, done: false },
      };
    }
    return {
      count: rows.length,
      line: savedLine(
        name,
        `${rows.length} ${rows.length === 1 ? "instance" : "instances"}`,
        testsFor(rows.map((row) => connectionTestKey("arr", row.id))),
      ),
    };
  };
  const sonarr = arrLine("sonarr", "Sonarr");
  const radarr = arrLine("radarr", "Radarr");
  const sportarr = arrLine("sportarr", "Sportarr");
  const instanceCount = sonarr.count + radarr.count + sportarr.count;
  const profileCount = (profiles ?? []).length;
  const providers = general?.enabled_providers ?? [];
  const useSeerr = general?.use_seerr ?? false;
  const translatorReady =
    (settings?.translator?.openrouter_api_key ?? "").length > 0;

  const libraryLines: SummaryLine[] = [sonarr.line, radarr.line, sportarr.line];

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
          .map((entry) => {
            const counted = `${entry.count} ${
              entry.count === 1 ? "server" : "servers"
            }`;
            // Plex has no Test in the wizard: its row comes from signing in
            // and picking one of the account's servers.
            return entry.kind === "plex"
              ? {
                  label: `${kindName(entry.kind)} connected (${counted})`,
                  done: true,
                }
              : savedLine(
                  kindName(entry.kind),
                  counted,
                  testsFor(
                    entry.ids.map((id) =>
                      connectionTestKey("media-server", id),
                    ),
                  ),
                );
          });

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
        onSuccess: async () => {
          await settleSetupComplete(client, true);
          clearPersistedStep();
          clearPersistedIntent();
          clearPersistedSelection();
          clearPersistedConnectionTests();
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
    <StepLayout
      title="You are all set"
      description={
        discoverPath
          ? "Search for any film or series, preview what is available and download it straight to this device. No library required."
          : instanceCount > 0
            ? "Your first library scan starts now, and Bazarr+ will begin looking for the subtitles it is missing."
            : "Nothing is connected yet, so Bazarr+ has no library to scan. Discover still works, and you can connect an instance whenever you like."
      }
      aside={
        <Stack gap="md">
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
              Bazarr+ could not save that setup is complete, so this screen
              comes back on the next load. Check that Bazarr+ is still running,
              then try again.
            </Alert>
          )}
        </Stack>
      }
      actions={
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
      }
    >
      <List spacing="sm" center>
        {lines.map((line) => (
          <SummaryItem key={line.label} label={line.label} done={line.done} />
        ))}
      </List>
    </StepLayout>
  );
};

export default FinishStep;
