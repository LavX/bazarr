import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import {
  Alert,
  Button,
  Container,
  SegmentedControl,
  Skeleton,
  Tabs,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faListCheck,
  faPlug,
  faRotate,
  faStore,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQuery } from "@tanstack/react-query";
import {
  usePluginCatalog,
  usePluginCheckUpdates,
  usePluginInstallations,
} from "@/apis/hooks/plugins";
import { useAppTitle } from "@/apis/hooks/site";
import { useSystemStatus } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { SearchBar } from "@/components/hub/SearchBar";
import {
  PluginCostCaps,
  usePluginConsentFlow,
} from "@/components/plugin-consent";
import { ActivityPanel } from "./ActivityPanel";
import {
  entryKind,
  inFilter,
  installationKind,
  installedRows,
  marketplaceRows,
  matchesQuery,
  OFFICIAL_PLUGIN_ID,
  OFFICIAL_SOURCE_ID,
  PluginRow,
  TYPE_FILTERS,
  typeFilterOf,
} from "./model";
import { InstalledPanel, MarketplacePanel, UpdatesPanel } from "./Panels";
import { SourceBand } from "./SourceBand";
import styles from "./PluginHub.module.scss";

type TabKey = "marketplace" | "installed" | "updates" | "activity";
const TABS: TabKey[] = ["marketplace", "installed", "updates", "activity"];

/** The page while its first answer is on the way, in the shape it will take. */
const LoadingPage: FunctionComponent = () => (
  <div role="status" aria-busy="true" aria-label="Loading the Plugin Hub">
    <Skeleton height={54} radius="lg" mb="md" />
    <Skeleton height={36} width={360} radius="sm" mb="lg" />
    <div style={{ display: "grid", gap: 16 }}>
      <Skeleton height={140} radius="md" />
      <Skeleton height={140} radius="md" />
    </div>
  </div>
);

/**
 * The spending caps the consent dialog shows for the AI engine, read from the Translation API. Asked
 * only while the catalog lists the engine, and only of a server that lists the capability, so a host
 * without the new runtime is never asked. When it cannot be read the dialog leaves the line out
 * rather than say no cap is set.
 */
function useCostCaps(enabled: boolean): PluginCostCaps | undefined {
  const caps = useQuery({
    queryKey: [QueryKeys.Translation, "plugin-hub-caps"],
    queryFn: async (): Promise<PluginCostCaps | null> => {
      const state = await withInlineErrors(() => api.translation.state());
      if (!state.capabilities?.includes("limits")) {
        return null;
      }
      const summary = await withInlineErrors(() => api.translation.summary());
      return {
        jobCap: summary.job_cap,
        monthCap: summary.month.cap,
        pluginId: OFFICIAL_PLUGIN_ID,
      };
    },
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  });
  return caps.data ?? undefined;
}

/** Plugin Hub: translators, translator back ends and services, from the catalog to the box. */
const PluginHubView: FunctionComponent = () => {
  useDocumentTitle(`Plugin Hub - ${useAppTitle()}`);
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab") as TabKey | null;
  const tab: TabKey =
    requested && TABS.includes(requested) ? requested : "marketplace";
  const filter = typeFilterOf(params.get("type"));
  const [query, setQuery] = useState("");

  const catalog = usePluginCatalog();
  const installations = usePluginInstallations();
  const status = useSystemStatus();
  const checkUpdates = usePluginCheckUpdates();

  const installed = useMemo(
    () => installations.data ?? [],
    [installations.data],
  );
  const hasAiEngine = (catalog.data?.entries ?? []).some(
    (entry) =>
      entry.provider_id === OFFICIAL_PLUGIN_ID &&
      entry.source === OFFICIAL_SOURCE_ID,
  );
  const costCaps = useCostCaps(hasAiEngine);
  const flow = usePluginConsentFlow({
    costCaps,
    // A finished install replaces the button that opened the dialog, and focus would fall to the
    // page. It goes to the tab in use instead, so the next keypress starts from a known place.
    onClosed: () => {
      if (
        document.activeElement === document.body ||
        document.activeElement === null
      ) {
        document
          .querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
          ?.focus();
      }
    },
  });

  const setParam = useCallback(
    (key: "tab" | "type", value: string, fallback: string) => {
      const updated = new URLSearchParams(params);
      if (value === fallback) {
        updated.delete(key);
      } else {
        updated.set(key, value);
      }
      setParams(updated, { replace: true });
    },
    [params, setParams],
  );

  const marketplace = useMemo(
    () =>
      marketplaceRows(catalog.data, installed, filter).filter((row) =>
        matchesQuery(row, query),
      ),
    [catalog.data, installed, filter, query],
  );
  const installedList = useMemo(
    () =>
      installedRows(catalog.data, installed, filter).filter((row) =>
        matchesQuery(row, query),
      ),
    [catalog.data, installed, filter, query],
  );
  // The badge counts every kind, so an update is not hidden by a filter that is looking elsewhere.
  const allUpdates = useMemo(
    () =>
      installedRows(catalog.data, installed, "all").filter(
        (row) => row.update !== null,
      ),
    [catalog.data, installed],
  );
  const updates = useMemo(
    () => allUpdates.filter((row) => inFilter(row.kind, filter)),
    [allUpdates, filter],
  );
  const pluginIds = useMemo(() => {
    const ids = new Set<string>();
    for (const item of installed) {
      if (inFilter(installationKind(item), "all")) ids.add(item.provider_id);
    }
    for (const entry of catalog.data?.entries ?? []) {
      if (inFilter(entryKind(entry), "all")) ids.add(entry.provider_id);
    }
    return ids;
  }, [installed, catalog.data]);

  const officialSource = catalog.data?.sources.find(
    (source) => source.id === OFFICIAL_SOURCE_ID,
  );
  const sandbox = status.data?.sandbox;

  const install = (row: PluginRow) => {
    if (!row.entry?.source) {
      return;
    }
    void flow.begin({
      action: "install",
      pluginId: row.id,
      name: row.name,
      source: row.entry.source,
      version: row.entry.version,
      kind: row.kind,
    });
  };
  const update = (row: PluginRow) =>
    void flow.begin({ action: "update", pluginId: row.id, name: row.name });
  const actions = {
    sandbox,
    onInstall: install,
    onUpdate: update,
    updating: flow.updating,
  };

  if (catalog.isLoading && !catalog.data) {
    return (
      <Container fluid px="md" className={styles.page}>
        <LoadingPage />
      </Container>
    );
  }
  if (!catalog.data) {
    return (
      <Container fluid px="md" className={styles.page}>
        <Alert
          color="red"
          role="alert"
          title="The Plugin Hub could not be loaded"
        >
          Bazarr+ did not answer. Check that it is running, then try again.
          <div>
            <Button
              mt="sm"
              size="xs"
              variant="default"
              loading={catalog.isFetching}
              onClick={() => void catalog.refetch()}
            >
              Try again
            </Button>
          </div>
        </Alert>
      </Container>
    );
  }

  const searching = query.trim() !== "";
  const showToolbar = tab !== "activity";

  return (
    <Container fluid px="md" className={styles.page}>
      {installations.isError && (
        <Alert color="red" role="alert" mb="md">
          What is installed could not be read, so this page may be out of date.
          <div>
            <Button
              mt="xs"
              size="xs"
              variant="default"
              loading={installations.isFetching}
              onClick={() => void installations.refetch()}
            >
              Try again
            </Button>
          </div>
        </Alert>
      )}
      <SourceBand source={officialSource} />
      <Tabs
        value={tab}
        keepMounted={false}
        onChange={(next) => next && setParam("tab", next, "marketplace")}
      >
        <Tabs.List>
          <Tabs.Tab
            value="marketplace"
            leftSection={<FontAwesomeIcon icon={faStore} />}
          >
            Marketplace
          </Tabs.Tab>
          <Tabs.Tab
            value="installed"
            leftSection={<FontAwesomeIcon icon={faPlug} />}
          >
            Installed
          </Tabs.Tab>
          <Tabs.Tab
            value="updates"
            leftSection={<FontAwesomeIcon icon={faRotate} />}
            rightSection={
              allUpdates.length > 0 ? (
                <span
                  aria-label={`${allUpdates.length} pending update${
                    allUpdates.length === 1 ? "" : "s"
                  }`}
                  style={{
                    display: "inline-flex",
                    minWidth: 18,
                    height: 18,
                    padding: "0 5px",
                    alignItems: "center",
                    justifyContent: "center",
                    borderRadius: 9,
                    fontSize: 11,
                    fontWeight: 600,
                    background: "var(--bz-stat-processing)",
                    color: "var(--bz-bg-elevated, #fff)",
                  }}
                >
                  {allUpdates.length}
                </span>
              ) : null
            }
          >
            Updates
          </Tabs.Tab>
          <Tabs.Tab
            value="activity"
            leftSection={<FontAwesomeIcon icon={faListCheck} />}
          >
            Activity
          </Tabs.Tab>
        </Tabs.List>
        {showToolbar && (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: 12,
              margin: "16px 0",
            }}
          >
            <div className={styles.toolbarTypes}>
              <SegmentedControl
                aria-label="Plugin type"
                size="xs"
                value={filter}
                onChange={(next) => setParam("type", next, "all")}
                data={TYPE_FILTERS}
              />
            </div>
            {tab !== "updates" && (
              <SearchBar
                value={query}
                onChange={setQuery}
                placeholder="Search plugins"
                ariaLabel="Search plugins"
              />
            )}
          </div>
        )}
        <Tabs.Panel value="marketplace" pt={showToolbar ? 0 : "lg"}>
          <MarketplacePanel
            rows={marketplace}
            filter={filter}
            searching={searching}
            {...actions}
          />
        </Tabs.Panel>
        <Tabs.Panel value="installed">
          <InstalledPanel
            rows={installedList}
            filter={filter}
            searching={searching}
            onBrowse={() => setParam("tab", "marketplace", "marketplace")}
            {...actions}
          />
        </Tabs.Panel>
        <Tabs.Panel value="updates">
          <UpdatesPanel
            rows={updates}
            filter={filter}
            elsewhere={allUpdates.length - updates.length}
            checking={checkUpdates.isPending}
            onCheck={() => checkUpdates.mutate()}
            {...actions}
          />
        </Tabs.Panel>
        <Tabs.Panel value="activity" pt="lg">
          <ActivityPanel pluginIds={pluginIds} />
        </Tabs.Panel>
      </Tabs>
      {flow.dialog}
    </Container>
  );
};

export default PluginHubView;
