import { FC, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { faMagnifyingGlass } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
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

/**
 * First Providers sub-stage. The user picks installable providers from the
 * catalog; installing them stages new code on disk, which requires a restart
 * to load. After the restart the wizard resumes via a hard redirect to /setup
 * (see redirectToSetup) and lands on the configure stage.
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

  const handleInstall = useCallback(async () => {
    const toInstall = choices.filter((choice) =>
      selected.includes(choice.providerId),
    );
    if (toInstall.length === 0) {
      return;
    }

    for (const choice of toInstall) {
      await install.mutateAsync({ manifest: choice.manifest });
    }

    setRestarting(true);
    onInstalledNeedsRestart();
    restart(undefined, {
      onSuccess: () => {
        startHealthPoll();
      },
    });
    // Some restart paths resolve before the connection drops; poll regardless.
    startHealthPoll();
  }, [
    choices,
    install,
    onInstalledNeedsRestart,
    restart,
    selected,
    startHealthPoll,
  ]);

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
          loading={install.isPending}
          disabled={selected.length === 0}
        >
          Install &amp; restart
        </Button>
      </Group>
    </Stack>
  );
};

export default ProviderInstallStage;
