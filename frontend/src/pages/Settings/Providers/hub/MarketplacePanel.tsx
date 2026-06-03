import { FunctionComponent, useCallback, useMemo, useState } from "react";
import {
  Button,
  Chip,
  FileButton,
  Group,
  SegmentedControl,
  Stack,
} from "@mantine/core";
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
import { CatalogCard } from "@/pages/Settings/Providers/hub/components/CatalogCard";
import { EmptyState } from "@/pages/Settings/Providers/hub/components/EmptyState";
import { SearchBar } from "@/pages/Settings/Providers/hub/components/SearchBar";
import { SourcesDrawer } from "@/pages/Settings/Providers/hub/SourcesDrawer";
import {
  getLatestCatalogEntry,
  parseManifest,
} from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface MarketplacePanelProps {
  catalog: ProviderHubCatalog | undefined;
  providers: ProviderHubInstallation[] | undefined;
}

type TrustFilter = "all" | "trusted" | "community";

function sourceNameFromInstallation(provider: ProviderHubInstallation) {
  const source = provider.manifest?.source as LooseObject | undefined;
  return (
    (typeof source?.name === "string" ? source.name : undefined) ??
    (typeof source?.repo === "string" ? source.repo : undefined) ??
    "Installed"
  );
}

function catalogEntryFromInstallation(
  provider: ProviderHubInstallation,
): ProviderHubCatalogEntry {
  const version =
    provider.staged_version ?? provider.active_version ?? "installed";
  return {
    source: sourceNameFromInstallation(provider),
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

  const installedSourceUsage = useMemo(() => {
    const usage: Record<string, number> = {};
    for (const p of providers ?? []) {
      const source = p.manifest?.source as LooseObject | undefined;
      const sourceName = source?.repo as string | undefined;
      if (sourceName) usage[sourceName] = (usage[sourceName] ?? 0) + 1;
    }
    return usage;
  }, [providers]);

  const latestPerProvider = useMemo(() => {
    const seen = new Map<string, ProviderHubCatalogEntry>();
    for (const entry of catalog?.entries ?? []) {
      const current = seen.get(entry.provider_id);
      if (!current) {
        seen.set(entry.provider_id, entry);
        continue;
      }
      const best = getLatestCatalogEntry(
        { sources: [], entries: [current, entry] },
        entry.provider_id,
      );
      if (best) seen.set(entry.provider_id, best);
    }
    return Array.from(seen.values());
  }, [catalog?.entries]);

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
    const installedOnlyEntries = (providers ?? [])
      .filter(
        (provider) =>
          !catalogProviderIds.has(provider.provider_id) &&
          provider.origin !== "local",
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
      const manifest = parseManifest(entry);
      if (manifest) install.mutate({ manifest });
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
        key={`${entry.provider_id}-${entry.version}`}
        entry={entry}
        installed={installedById.get(entry.provider_id) ?? null}
        onInstall={handleInstall}
        isInstalling={
          install.isPending &&
          install.variables?.manifest?.provider_id === entry.provider_id
        }
        onTest={(providerId) => testProvider.mutate(providerId)}
        onUninstall={(providerId) => uninstall.mutate(providerId)}
        isTesting={
          testProvider.isPending && testProvider.variables === entry.provider_id
        }
        isLocal={isLocal}
      />
    ),
    [installedById, handleInstall, install, testProvider, uninstall],
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
              if (file) installLocal.mutate(file);
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
