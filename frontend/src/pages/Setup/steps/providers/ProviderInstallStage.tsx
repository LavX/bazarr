import { FC, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Group,
  List,
  Loader,
  ScrollArea,
  Stack,
  Text,
  TextInput,
  ThemeIcon,
  Title,
} from "@mantine/core";
import {
  faCircleCheck,
  faCircleExclamation,
  faMagnifyingGlass,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { AxiosError } from "axios";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useSystem,
} from "@/apis/hooks";
import api from "@/apis/raw";
import type {
  ProviderHubCatalogEntry,
  ProviderHubManifest,
} from "@/apis/raw/providerHub";
import { parseManifest } from "@/pages/Settings/Providers/hub/utils";
import { redirectToSetup } from "./redirect";

// How often we re-check the backend after the restart. The backend drops
// connections while it bounces, so failed polls are expected and ignored.
const HEALTH_POLL_INTERVAL_MS = 5000;

export interface ProviderInstallStageProps {
  hasInstalled: boolean;
  // Called after installs succeed and a restart is kicked off. ProvidersStep
  // uses this to remember it is mid-restart (the overlay owns the resume).
  onInstalledNeedsRestart: () => void;
  // Switch to the configure sub-stage without restarting (providers already
  // installed). Only surfaced when hasInstalled is true.
  onUseInstalled: () => void;
  onBack?: () => void;
}

interface CatalogChoice {
  providerId: string;
  name: string;
  description: string | undefined;
  manifest: ProviderHubManifest;
}

interface InstallOutcome {
  providerId: string;
  name: string;
  staged: boolean;
  error?: string;
}

function describeEntry(
  entry: ProviderHubCatalogEntry,
  manifest: ProviderHubManifest,
): string | undefined {
  const desc = (manifest as LooseObject)?.description;
  if (typeof desc === "string" && desc.length > 0) {
    return desc;
  }
  return entry.source ?? entry.source_name ?? undefined;
}

// Why one provider did not install, in the backend's own words where it gave
// any. A failure the user cannot name is a failure they cannot act on.
function describeInstallError(reason: unknown): string {
  if (reason instanceof AxiosError) {
    const data = reason.response?.data as { message?: string } | undefined;
    if (data?.message) {
      return data.message;
    }
  }
  if (reason instanceof Error && reason.message) {
    return reason.message;
  }
  return "The install failed and Bazarr+ gave no reason.";
}

/**
 * First Providers sub-stage. The user picks installable providers from the
 * catalog; installing them stages new code on disk, which requires a restart
 * to load. After the restart the wizard resumes via a hard redirect to /setup
 * (see redirectToSetup) and lands on the configure stage.
 *
 * Every selected provider is attempted, whatever the others do. The run used to
 * be a loop of awaited installs with no catch: the first rejection ended it,
 * every later provider was never tried, the rejection escaped an async callback
 * unhandled and the restart never happened, so the button simply stopped
 * spinning. Now the outcomes are collected per provider and shown, the restart
 * is gated on at least one provider having staged, and the failures can be
 * retried on their own without re-installing what already worked.
 */
const ProviderInstallStage: FC<ProviderInstallStageProps> = ({
  hasInstalled,
  onInstalledNeedsRestart,
  onUseInstalled,
  onBack,
}) => {
  const { data: catalog } = useProviderHubCatalog();
  const install = useProviderHubInstall();
  const { restart } = useSystem();

  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [restarting, setRestarting] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [outcomes, setOutcomes] = useState<InstallOutcome[] | null>(null);
  // Mirrors `outcomes` so a retry can fold its results into the previous run
  // without reading state through an updater (which StrictMode double-invokes).
  const outcomesRef = useRef<InstallOutcome[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const choices: CatalogChoice[] = (catalog?.entries ?? [])
    .map((entry) => {
      const manifest = parseManifest(entry);
      if (!manifest) {
        return null;
      }
      return {
        providerId: entry.provider_id,
        name:
          entry.name ??
          (typeof (manifest as LooseObject).name === "string"
            ? ((manifest as LooseObject).name as string)
            : entry.provider_id),
        description: describeEntry(entry, manifest),
        manifest,
      } satisfies CatalogChoice;
    })
    .filter((choice): choice is CatalogChoice => choice !== null)
    // Hide internal smoketest providers (e.g. SmokeHub) from the first-run catalog.
    .filter(
      (choice) =>
        !/smoketest/i.test(
          `${choice.providerId} ${choice.name} ${choice.description ?? ""}`,
        ),
    );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) {
      return choices;
    }
    return choices.filter((choice) =>
      `${choice.name} ${choice.description ?? ""}`.toLowerCase().includes(q),
    );
    // choices is rebuilt from catalog each render; filtering it is cheap.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, query]);

  const toggle = useCallback((providerId: string) => {
    setSelected((current) =>
      current.includes(providerId)
        ? current.filter((id) => id !== providerId)
        : [...current, providerId],
    );
  }, []);

  // Poll the backend until it answers, then hard-redirect into the wizard.
  const startHealthPoll = useCallback(() => {
    if (pollRef.current !== null) {
      return;
    }
    pollRef.current = setInterval(() => {
      void api.system
        .status()
        .then(() => {
          if (pollRef.current !== null) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
          redirectToSetup();
        })
        .catch(() => {
          // Backend still down; keep polling.
        });
    }, HEALTH_POLL_INTERVAL_MS);
  }, []);

  useEffect(() => {
    return () => {
      if (pollRef.current !== null) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  const beginRestart = useCallback(() => {
    setRestarting(true);
    onInstalledNeedsRestart();
    restart(undefined, {
      onSuccess: () => {
        startHealthPoll();
      },
    });
    // Some restart paths resolve before the connection drops; poll regardless.
    startHealthPoll();
  }, [onInstalledNeedsRestart, restart, startHealthPoll]);

  // Runs `targets` together and folds the results into `outcomes`, keeping any
  // provider that is not in this run exactly as it was. That is what makes a
  // retry of the failed subset additive rather than a fresh verdict.
  const runInstall = useCallback(
    async (targets: CatalogChoice[]) => {
      if (targets.length === 0) {
        return;
      }
      setInstalling(true);
      const results = await Promise.allSettled(
        targets.map((choice) =>
          install.mutateAsync({ manifest: choice.manifest }),
        ),
      );
      const fresh: InstallOutcome[] = targets.map((choice, index) => {
        const result = results[index];
        return result.status === "fulfilled"
          ? { providerId: choice.providerId, name: choice.name, staged: true }
          : {
              providerId: choice.providerId,
              name: choice.name,
              staged: false,
              error: describeInstallError(result.reason),
            };
      });

      const byId = new Map(
        outcomesRef.current.map((outcome) => [outcome.providerId, outcome]),
      );
      for (const outcome of fresh) {
        byId.set(outcome.providerId, outcome);
      }
      const merged = Array.from(byId.values());
      outcomesRef.current = merged;
      setOutcomes(merged);
      setInstalling(false);

      // Nothing staged means nothing new to load, so a restart would only cost
      // the user a minute and tell them nothing. Leave the failures on screen.
      if (merged.every((outcome) => !outcome.staged)) {
        return;
      }
      if (fresh.every((outcome) => outcome.staged)) {
        // A clean run has nothing to report, so go straight to the restart.
        beginRestart();
      }
    },
    [beginRestart, install],
  );

  const handleInstall = useCallback(() => {
    outcomesRef.current = [];
    setOutcomes(null);
    return runInstall(
      choices.filter((choice) => selected.includes(choice.providerId)),
    );
  }, [choices, runInstall, selected]);

  const failed = (outcomes ?? []).filter((outcome) => !outcome.staged);
  const staged = (outcomes ?? []).filter((outcome) => outcome.staged);

  const handleRetryFailed = useCallback(() => {
    const retry = choices.filter((choice) =>
      failed.some((outcome) => outcome.providerId === choice.providerId),
    );
    return runInstall(retry);
  }, [choices, failed, runInstall]);

  if (restarting) {
    return (
      <Stack align="center" justify="center" gap="lg" mih={260} py="xl">
        <Loader size="lg" />
        <Stack gap={6} align="center">
          <Title order={3}>Restarting Bazarr+</Title>
          <Text ta="center" c="dimmed" maw={440}>
            Finishing provider installation. This page reloads automatically
            once Bazarr+ is back, usually within a minute.
          </Text>
        </Stack>
        <Button variant="subtle" onClick={() => redirectToSetup()}>
          Taking too long? Reload now
        </Button>
      </Stack>
    );
  }

  if (outcomes !== null) {
    return (
      <Stack gap="lg">
        <Stack gap="xs">
          <Title order={2}>
            {staged.length > 0
              ? "Some providers did not install"
              : "No provider installed"}
          </Title>
          <Text c="dimmed">
            {staged.length > 0
              ? "Every provider you picked was attempted. The ones that installed are staged and will load when Bazarr+ restarts."
              : "Every provider you picked was attempted and none of them installed, so there is nothing to restart for."}
          </Text>
        </Stack>

        <List spacing="sm" center>
          {outcomes.map((outcome) => (
            <List.Item
              key={outcome.providerId}
              icon={
                <ThemeIcon
                  color={outcome.staged ? "green" : "red"}
                  size={20}
                  radius="xl"
                >
                  <FontAwesomeIcon
                    icon={outcome.staged ? faCircleCheck : faCircleExclamation}
                    size="xs"
                  />
                </ThemeIcon>
              }
            >
              <Text>{outcome.name}</Text>
              <Text size="sm" c="dimmed">
                {outcome.staged
                  ? "Installed, waiting for the restart"
                  : outcome.error}
              </Text>
            </List.Item>
          ))}
        </List>

        <Group justify="space-between">
          <Button
            variant="default"
            onClick={() => void handleRetryFailed()}
            loading={installing}
            disabled={failed.length === 0}
          >
            Retry the{" "}
            {failed.length === 1 ? "failure" : `${failed.length} failures`}
          </Button>
          {staged.length > 0 ? (
            <Button onClick={beginRestart}>Restart and continue</Button>
          ) : (
            <Button
              variant="subtle"
              onClick={() => {
                outcomesRef.current = [];
                setOutcomes(null);
              }}
            >
              Pick different providers
            </Button>
          )}
        </Group>
      </Stack>
    );
  }

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>Add subtitle providers</Title>
        <Text c="dimmed">
          Pick the providers you want Bazarr+ to search. Installing providers
          stages new code, so Bazarr+ needs to restart once to load them. The
          wizard will pick up where it left off after the restart.
        </Text>
      </Stack>

      {choices.length === 0 ? (
        <Alert color="gray" title="No providers available">
          No installable providers were found in the catalog.
        </Alert>
      ) : (
        <Stack gap="sm">
          <TextInput
            placeholder="Search providers"
            leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
            value={query}
            onChange={(e) => setQuery(e.currentTarget.value)}
          />
          <ScrollArea.Autosize mah={320} type="auto" offsetScrollbars>
            <Stack gap="sm" pr="sm">
              {visible.length === 0 ? (
                <Text c="dimmed" size="sm" py="md" ta="center">
                  No providers match &ldquo;{query}&rdquo;.
                </Text>
              ) : (
                visible.map((choice) => (
                  <Checkbox
                    key={choice.providerId}
                    label={choice.name}
                    description={choice.description}
                    checked={selected.includes(choice.providerId)}
                    onChange={() => toggle(choice.providerId)}
                  />
                ))
              )}
            </Stack>
          </ScrollArea.Autosize>
          {selected.length > 0 && (
            <Text size="sm" c="dimmed">
              {selected.length} selected
            </Text>
          )}
        </Stack>
      )}

      <Group justify="space-between">
        <Group gap="sm">
          {onBack && (
            <Button variant="default" onClick={onBack}>
              Back
            </Button>
          )}
          {hasInstalled && (
            <Button variant="subtle" onClick={onUseInstalled}>
              Use already-installed providers
            </Button>
          )}
        </Group>
        <Button
          onClick={() => void handleInstall()}
          loading={installing}
          disabled={selected.length === 0}
        >
          Install &amp; restart
        </Button>
      </Group>
    </Stack>
  );
};

export default ProviderInstallStage;
