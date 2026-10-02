import { FunctionComponent, useCallback, useMemo, useState } from "react";
import {
  Button,
  Chip,
  FileButton,
  Group,
  SegmentedControl,
  Stack,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import {
  faSliders,
  faStore,
  faUpload,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useProviderHubInstall,
  useProviderHubInstallLocal,
  useProviderHubTest,
  useProviderHubUninstall,
} from "@/apis/hooks";
import type {
  ProviderHubCatalog,
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { notification } from "@/modules/notification";
import { CatalogCard } from "@/pages/Settings/Providers/hub/components/CatalogCard";
import { EmptyState } from "@/pages/Settings/Providers/hub/components/EmptyState";
import { SearchBar } from "@/pages/Settings/Providers/hub/components/SearchBar";
import { SourcesDrawer } from "@/pages/Settings/Providers/hub/SourcesDrawer";
import {
  getLatestCatalogEntry,
  parseManifest,
} from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

// The backend refuses a larger package (MAX_LOCAL_PACKAGE_SIZE in
// provider_hub/service.py); saying so here saves uploading it first.
const MAX_LOCAL_PACKAGE_MIB = 100;
const MAX_LOCAL_PACKAGE_SIZE = MAX_LOCAL_PACKAGE_MIB * 1024 * 1024;

interface MarketplacePanelProps {
  catalog: ProviderHubCatalog | undefined;
  providers: ProviderHubInstallation[] | undefined;
}

type TrustFilter = "all" | "trusted" | "community";

function cardKey(sourceId: string | undefined, providerId: string) {
  return `${sourceId ?? ""}\u0000${providerId}`;
}

// The source is the one Bazarr+ bound the install to, never the manifest's own
// label, so an install whose source is unknown or gone says so.
function catalogEntryFromInstallation(
  provider: ProviderHubInstallation,
): ProviderHubCatalogEntry {
  const version =
    provider.staged_version ?? provider.active_version ?? "installed";
  return {
    source: provider.source_id ?? undefined,
    source_name: provider.source_name ?? "Unknown source",
    provider_id: provider["provider_id"],
    name: provider.name,
    version,
    trusted: Boolean(provider.trusted),
    manifest: provider.manifest ?? null,
  };
}

export const MarketplacePanel: FunctionComponent<MarketplacePanelProps> = ({
  catalog,
  providers,
}) => {
  const [query, setQuery] = useState("");
  const [trustFilter, setTrustFilter] = useState<TrustFilter>("all");
  const [activeSources, setActiveSources] = useState<string[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const install = useProviderHubInstall();
  const installLocal = useProviderHubInstallLocal();
  const testProvider = useProviderHubTest();
  const uninstall = useProviderHubUninstall();

  const sources = catalog?.sources ?? [];

  const installedById = useMemo(() => {
    const map = new Map<string, ProviderHubInstallation>();
    for (const p of providers ?? []) {
      map.set(p.provider_id, p);
    }
    return map;
  }, [providers]);

  // Keyed by the source id each install is bound to.
  const installedSourceUsage = useMemo(() => {
    const usage: Record<string, number> = {};
    for (const p of providers ?? []) {
      if (p.source_id) usage[p.source_id] = (usage[p.source_id] ?? 0) + 1;
    }
    return usage;
  }, [providers]);

  // One card per source and provider: an install belongs to one source, and
  // another source's version of the same id is a different plugin.
  const latestPerProvider = useMemo(() => {
    const seen = new Map<string, ProviderHubCatalogEntry>();
    for (const entry of catalog?.entries ?? []) {
      const key = cardKey(entry.source, entry.provider_id);
      const current = seen.get(key);
      if (!current) {
        seen.set(key, entry);
        continue;
      }
      const best = getLatestCatalogEntry(
        { sources: [], entries: [current, entry] },
        entry.provider_id,
      );
      if (best) seen.set(key, best);
    }
    return Array.from(seen.values());
  }, [catalog?.entries]);

  const installedFor = useCallback(
    (entry: ProviderHubCatalogEntry) => {
      const installed = installedById.get(entry.provider_id);
      if (!installed) return null;
      // An install made before installs recorded their source is matched by
      // id until the next catalog refresh binds it.
      if (!installed.source_bound) return installed;
      return (installed.source_id ?? undefined) === entry.source
        ? installed
        : null;
    },
    [installedById],
  );

  // Providers installed from an uploaded package. They own their card (built from
  // the installation, not a catalog entry) and are excluded from the marketplace
  // list so a local override is never shown as a catalog card.
  const localProviderIds = useMemo(
    () =>
      new Set(
        (providers ?? [])
          .filter((provider) => provider.origin === "local")
          .map((provider) => provider.provider_id),
      ),
    [providers],
  );

  const localEntries = useMemo(
    () =>
      (providers ?? [])
        .filter((provider) => provider.origin === "local")
        .map(catalogEntryFromInstallation),
    [providers],
  );

  const marketplaceEntries = useMemo(() => {
    const catalogProviderIds = new Set(
      latestPerProvider.map((entry) => entry.provider_id),
    );
    const catalogCards = new Set(
      latestPerProvider.map((entry) =>
        cardKey(entry.source, entry.provider_id),
      ),
    );
    const installedOnlyEntries = (providers ?? [])
      .filter(
        (provider) =>
          provider.origin !== "local" &&
          (provider.source_bound
            ? !catalogCards.has(
                cardKey(provider.source_id ?? undefined, provider.provider_id),
              )
            : !catalogProviderIds.has(provider.provider_id)),
      )
      .map(catalogEntryFromInstallation);

    const catalogEntries = latestPerProvider.filter(
      (entry) => !localProviderIds.has(entry.provider_id),
    );
    return [...catalogEntries, ...installedOnlyEntries];
  }, [latestPerProvider, providers, localProviderIds]);

  const matchesFilters = useCallback(
    (entry: ProviderHubCatalogEntry) => {
      if (trustFilter === "trusted" && !entry.trusted) return false;
      if (trustFilter === "community" && entry.trusted) return false;
      if (activeSources.length > 0) {
        const sn = entry.source ?? entry.source_name ?? "";
        if (!activeSources.includes(sn)) return false;
      }
      const q = query.trim().toLowerCase();
      if (!q) return true;
      const haystack = [
        entry.name,
        entry.provider_id,
        entry.source,
        entry.source_name,
        (parseManifest(entry)?.description as string) ?? "",
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    },
    [query, trustFilter, activeSources],
  );

  const handleInstall = useCallback(
    (entry: ProviderHubCatalogEntry) => {
      if (!entry.source || !parseManifest(entry)) return;
      install.mutate({
        source: entry.source,
        provider_id: entry.provider_id,
        version: entry.version,
      });
    },
    [install],
  );

  const marketplaceCards = useMemo(
    () => marketplaceEntries.filter(matchesFilters),
    [marketplaceEntries, matchesFilters],
  );
  const localCards = useMemo(
    () => localEntries.filter(matchesFilters),
    [localEntries, matchesFilters],
  );

  const renderCard = useCallback(
    (entry: ProviderHubCatalogEntry, isLocal: boolean) => (
      <CatalogCard
        key={`${entry.source ?? ""}-${entry.provider_id}-${entry.version}`}
        entry={entry}
        installed={
          isLocal
            ? (installedById.get(entry.provider_id) ?? null)
            : installedFor(entry)
        }
        onInstall={handleInstall}
        isInstalling={
          install.isPending &&
          install.variables !== undefined &&
          "source" in install.variables &&
          install.variables.source === entry.source &&
          install.variables.provider_id === entry.provider_id
        }
        onTest={(providerId) => testProvider.mutate(providerId)}
        onUninstall={(providerId) => uninstall.mutate(providerId)}
        isTesting={
          testProvider.isPending && testProvider.variables === entry.provider_id
        }
        isLocal={isLocal}
      />
    ),
    [
      installedById,
      installedFor,
      handleInstall,
      install,
      testProvider,
      uninstall,
    ],
  );

  const noSources =
    sources.length === 0 &&
    marketplaceEntries.length === 0 &&
    localEntries.length === 0;
  const noResults =
    !noSources && marketplaceCards.length === 0 && localCards.length === 0;

  return (
    <Stack gap="md">
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.eyebrow}>Marketplace</div>
          <h2 className={styles.panelTitle}>Browse the catalog</h2>
          <p className={styles.panelDescription}>
            Browse and manage Provider Hub plugins from your configured catalog
            sources. Installed plugins can be tested or removed here.
          </p>
        </div>
        <Group gap="xs">
          <FileButton
            accept=".zip,application/zip"
            onChange={(file) => {
              if (!file) return;
              if (file.size > MAX_LOCAL_PACKAGE_SIZE) {
                showNotification(
                  notification.warn(
                    "Package is too large",
                    `${file.name} is larger than ${MAX_LOCAL_PACKAGE_MIB} MiB.`,
                  ),
                );
                return;
              }
              installLocal.mutate(file);
            }}
          >
            {(props) => (
              <Button
                {...props}
                variant="default"
                leftSection={<FontAwesomeIcon icon={faUpload} />}
                loading={installLocal.isPending}
              >
                Install local package
              </Button>
            )}
          </FileButton>
          <Button
            variant="default"
            leftSection={<FontAwesomeIcon icon={faSliders} />}
            onClick={() => setDrawerOpen(true)}
          >
            Manage sources ({sources.length})
          </Button>
        </Group>
      </div>

      <Group className={styles.toolbar}>
        <SearchBar
          value={query}
          onChange={setQuery}
          placeholder="Search providers"
          ariaLabel="Search marketplace"
        />
        <SegmentedControl
          value={trustFilter}
          onChange={(v) => setTrustFilter(v as TrustFilter)}
          data={[
            { label: "All", value: "all" },
            { label: "Trusted", value: "trusted" },
            { label: "Community", value: "community" },
          ]}
          size="xs"
        />
        {sources.length > 1 && (
          <Chip.Group
            multiple
            value={activeSources}
            onChange={setActiveSources}
          >
            <Group gap={6}>
              {sources.map((s) => (
                <Chip key={s.name} value={s.name} size="xs" variant="light">
                  {s.name}
                </Chip>
              ))}
            </Group>
          </Chip.Group>
        )}
      </Group>

      {noSources ? (
        <EmptyState
          icon={faStore}
          title="No catalog sources yet"
          body="Add your first GitHub catalog source to start browsing community providers."
          action={
            <Button variant="light" onClick={() => setDrawerOpen(true)}>
              Add a source
            </Button>
          }
        />
      ) : noResults ? (
        <EmptyState
          icon={faStore}
          title="No matching providers"
          body="Try a different search term or clear the active filters."
        />
      ) : (
        <>
          {marketplaceCards.length > 0 && (
            <div className={styles.cardGrid}>
              {marketplaceCards.map((entry) => renderCard(entry, false))}
            </div>
          )}
          {localCards.length > 0 && (
            <Stack gap="md">
              <div>
                <div className={styles.eyebrow}>Local</div>
                <h3 className={styles.panelTitle} style={{ fontSize: 18 }}>
                  Local / manually installed
                </h3>
                <p className={styles.panelDescription}>
                  Providers installed from an uploaded package rather than a
                  catalog source. They are never trusted and can never replace a
                  built-in provider.
                </p>
              </div>
              <div className={styles.cardGrid}>
                {localCards.map((entry) => renderCard(entry, true))}
              </div>
            </Stack>
          )}
        </>
      )}

      <SourcesDrawer
        opened={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        sources={sources}
        installedSourceUsage={installedSourceUsage}
      />
    </Stack>
  );
};
