import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Button, Chip, Group, SegmentedControl, Stack } from "@mantine/core";
import { faSliders, faStore } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useProviderHubInstall } from "@/apis/hooks";
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

export const MarketplacePanel: FunctionComponent<MarketplacePanelProps> = ({
  catalog,
  providers,
}) => {
  const [query, setQuery] = useState("");
  const [trustFilter, setTrustFilter] = useState<TrustFilter>("all");
  const [activeSources, setActiveSources] = useState<string[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const install = useProviderHubInstall();

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

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return latestPerProvider.filter((entry) => {
      if (trustFilter === "trusted" && !entry.trusted) return false;
      if (trustFilter === "community" && entry.trusted) return false;
      if (activeSources.length > 0) {
        const sn = entry.source ?? entry.source_name ?? "";
        if (!activeSources.includes(sn)) return false;
      }
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
    });
  }, [latestPerProvider, query, trustFilter, activeSources]);

  const handleInstall = useCallback(
    (entry: ProviderHubCatalogEntry) => {
      const manifest = parseManifest(entry);
      if (manifest) install.mutate({ manifest });
    },
    [install],
  );

  const noSources = sources.length === 0;
  const noResults = !noSources && filtered.length === 0;

  return (
    <Stack gap="md">
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.eyebrow}>Marketplace</div>
          <h2 className={styles.panelTitle}>Browse the catalog</h2>
          <p className={styles.panelDescription}>
            Discover community-maintained subtitle providers from your
            configured catalog sources. Install one to add it to your provider
            list.
          </p>
        </div>
        <Button
          variant="default"
          leftSection={<FontAwesomeIcon icon={faSliders} />}
          onClick={() => setDrawerOpen(true)}
        >
          Manage sources ({sources.length})
        </Button>
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
        <div className={styles.cardGrid}>
          {filtered.map((entry) => (
            <CatalogCard
              key={`${entry.provider_id}-${entry.version}`}
              entry={entry}
              installed={installedById.get(entry.provider_id) ?? null}
              onInstall={handleInstall}
              isInstalling={install.isPending}
            />
          ))}
        </div>
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
