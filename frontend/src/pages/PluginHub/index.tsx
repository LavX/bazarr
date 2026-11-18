import {
  FunctionComponent,
  useCallback,
  useMemo,
  useRef,
  useState,
} from "react";
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
import { showNotification } from "@mantine/notifications";
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
  usePluginDismissReview,
  usePluginInstallations,
  usePluginJobs,
  usePluginRemove,
  usePluginRepair,
  usePluginSetEnabled,
  usePluginUpdatesNeedingConsent,
} from "@/apis/hooks/plugins";
import { useAppTitle } from "@/apis/hooks/site";
import { useSystemStatus } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { RestartBanner } from "@/components/hub/RestartBanner";
import { SearchBar } from "@/components/hub/SearchBar";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import {
  PluginCostCaps,
  usePluginConsentFlow,
} from "@/components/plugin-consent";
import { needsPassword, reasonOf } from "@/components/plugin-consent/refusal";
import { notification } from "@/modules/notification";
import { explainFailure } from "./components/failure";
import { LogDialog } from "./components/LogDialog";
import { RemoveDialog } from "./components/RemoveDialog";
import { ActivityPanel } from "./ActivityPanel";
import {
  entryKind,
  inFilter,
  installationKind,
  installedRows,
  isWaitingUpdate,
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
import { openJobs } from "./status";
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

  const jobs = usePluginJobs();
  const open = useMemo(() => openJobs(jobs.data), [jobs.data]);
  const setEnabled = usePluginSetEnabled();
  const repair = usePluginRepair();
  const remove = usePluginRemove();
  const dismissReview = usePluginDismissReview();
  const runGated = useGatedAction();
  const [working, setWorking] = useState<string | null>(null);
  const [logOf, setLogOf] = useState<PluginRow | null>(null);
  const [removing, setRemoving] = useState<PluginRow | null>(null);
  const [removeProblem, setRemoveProblem] = useState("");
  // Focus goes back to the card's actions button when a dialog opened from its menu is closed. The menu
  // item that was used is gone by then, so the button that stays is the one remembered.
  const lastTrigger = useRef<HTMLElement | null>(null);
  const rememberTrigger = (row: PluginRow) => {
    lastTrigger.current = document.querySelector<HTMLElement>(
      `[data-plugin-actions="${CSS.escape(row.id)}"]`,
    );
  };
  const restoreFocus = () => {
    const trigger = lastTrigger.current;
    if (trigger && document.body.contains(trigger)) {
      trigger.focus();
      return;
    }
    // The card is gone (it was removed), so focus goes to the tab in use, where the next keypress starts.
    document
      .querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
      ?.focus();
  };

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
  // An update that is staged and waits for a restart is no longer on offer, but it is not done either, so
  // it stays on the tab and in its count until the restart has put it in place.
  const allWaiting = useMemo(
    () =>
      installedRows(catalog.data, installed, "all").filter((row) =>
        isWaitingUpdate(row.installation),
      ),
    [catalog.data, installed],
  );
  const waiting = useMemo(
    () => allWaiting.filter((row) => inFilter(row.kind, filter)),
    [allWaiting, filter],
  );
  const updateCount = allUpdates.length + allWaiting.length;
  // An available update is held for the person's agreement when what it adds needs consent. Only what the
  // server says is shown; the step after staging that asks again for an address the plugin works out for
  // itself is not reported yet, so it has no state here.
  const needsConsent = usePluginUpdatesNeedingConsent(
    useMemo(() => allUpdates.map((row) => row.id), [allUpdates]),
  );
  // Every change the server says needs a restart, whatever the plugin's type or the filter in use.
  const restartCount = useMemo(
    () => installed.filter((item) => item.needs_restart === true).length,
    [installed],
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
  /** One request that starts code, behind the password question, asked again once if the server still wanted it. */
  const gated = async (send: () => Promise<unknown>, label: string) => {
    try {
      return await runGated(async () => void (await send()), label);
    } catch (error) {
      if (!needsPassword(error)) {
        throw error;
      }
      return runGated(async () => void (await send()), label);
    }
  };
  /**
   * Runs one action for one plugin and says in words what happened. An action behind the password question
   * that was not confirmed returns false: nothing was sent, so nothing is said.
   */
  const perform = async (
    row: PluginRow,
    run: () => Promise<unknown>,
    done: string,
    failed: string,
    // A refusal from the server is already a sentence for a person. Only a failed job is put in the fixed words.
    explain = false,
  ) => {
    if (working !== null) {
      return false;
    }
    setWorking(row.id);
    try {
      if ((await run()) === false) {
        return false;
      }
      showNotification(notification.info(done, row.name));
      return true;
    } catch (error) {
      const reason = reasonOf(error);
      const copy = explain ? explainFailure(reason) : null;
      showNotification(
        notification.error(
          failed,
          copy
            ? `${copy.cause} ${copy.next}`
            : reason || `${row.name} did not change.`,
        ),
      );
      return false;
    } finally {
      setWorking(null);
    }
  };
  const toggle = (row: PluginRow) => {
    const turnOn = row.installation?.enabled === false;
    void perform(
      row,
      () => setEnabled.mutateAsync({ id: row.id, enabled: turnOn }),
      turnOn ? "Turned on" : "Turned off",
      turnOn ? "Could not turn it on" : "Could not turn it off",
    );
  };
  const doRepair = (row: PluginRow) =>
    void perform(
      row,
      () => gated(() => repair.mutateAsync(row.id), `repair ${row.name}`),
      "Repaired",
      "The repair did not finish",
      true,
    );
  const askRemove = (row: PluginRow) => {
    rememberTrigger(row);
    setRemoveProblem("");
    setRemoving(row);
  };
  const confirmRemove = async (keepData: boolean) => {
    if (!removing || working !== null) {
      return;
    }
    setWorking(removing.id);
    setRemoveProblem("");
    try {
      // Removing a plugin can delete its settings and keys, so the server asks for the password first, as it
      // does for a repair. A prompt that is closed leaves the dialog as it was and says nothing.
      const confirmed = await gated(
        () => remove.mutateAsync({ id: removing.id, keepData }),
        `remove ${removing.name}`,
      );
      if (confirmed === false) {
        return;
      }
      showNotification(
        notification.info(
          keepData ? "Removed, its data is kept" : "Removed with its data",
          removing.name,
        ),
      );
      setRemoving(null);
    } catch (error) {
      setRemoveProblem(
        reasonOf(error) || `${removing.name} could not be removed.`,
      );
    } finally {
      setWorking(null);
    }
  };
  const showLog = (row: PluginRow) => {
    rememberTrigger(row);
    setLogOf(row);
  };
  const actions = {
    sandbox,
    onInstall: install,
    onUpdate: update,
    updating: flow.updating,
    needsConsent,
    openJobs: open,
    working,
    dismissing: dismissReview.isPending
      ? (dismissReview.variables ?? null)
      : null,
    onToggle: toggle,
    onRepair: doRepair,
    onLog: showLog,
    onRemove: askRemove,
    onDismissReview: (row: PluginRow) => dismissReview.mutate(row.id),
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
      <RestartBanner count={restartCount} />
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
              updateCount > 0 ? (
                <span
                  aria-label={`${updateCount} pending update${
                    updateCount === 1 ? "" : "s"
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
                  {updateCount}
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
            waiting={waiting}
            filter={filter}
            held={updates.filter((row) => needsConsent.has(row.id)).length}
            elsewhere={
              allUpdates.length -
              updates.length +
              (allWaiting.length - waiting.length)
            }
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
      <RemoveDialog
        row={removing}
        busy={working !== null && working === removing?.id}
        problem={removeProblem}
        onConfirm={(keepData) => void confirmRemove(keepData)}
        onClose={() => setRemoving(null)}
        onClosed={restoreFocus}
      />
      <LogDialog
        row={logOf}
        onClose={() => setLogOf(null)}
        onClosed={restoreFocus}
      />
    </Container>
  );
};

export default PluginHubView;
