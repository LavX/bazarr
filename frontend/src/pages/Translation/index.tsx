import { FunctionComponent, useState } from "react";
import { useSearchParams } from "react-router";
import { Alert, Button, Container, Skeleton, Tabs } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faClockRotateLeft,
  faLanguage,
  faSliders,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useAppTitle } from "@/apis/hooks/site";
import {
  useTranslationState,
  useTranslationSummary,
} from "@/apis/hooks/translation";
import { RestartBanner } from "@/components/hub/RestartBanner";
import type {
  TranslationCapability,
  TranslationState,
} from "@/types/translation";
import ActivityPanel from "./ActivityPanel";
import DefaultsPanel from "./DefaultsPanel";
import EnginePanel from "./EnginePanel";
import EngineSetup from "./EngineSetup";
import { isFirstRun, orderedEngines } from "./model";
import QuickStart from "./QuickStart";
import StatusBand from "./StatusBand";
import { useRestartAware } from "./useEngineInstall";
import styles from "./Translation.module.scss";

type TabKey = "engine" | "defaults" | "activity";
const TABS: TabKey[] = ["engine", "defaults", "activity"];

/** The page while its first answer is on the way, in the shape it will take. */
const LoadingPage: FunctionComponent = () => (
  <div aria-busy="true" aria-label="Loading translation">
    <Skeleton height={54} radius="lg" mb="md" />
    <Skeleton height={36} width={320} radius="sm" mb="lg" />
    <div className={styles.layout}>
      <div style={{ display: "grid", gap: 8 }}>
        <Skeleton height={96} radius="md" />
        <Skeleton height={96} radius="md" />
        <Skeleton height={64} radius="md" />
      </div>
      <div style={{ display: "grid", gap: 8 }}>
        <Skeleton height={28} width={280} radius="sm" />
        <Skeleton height={68} radius="md" />
        <Skeleton height={68} radius="md" />
      </div>
    </div>
  </div>
);

const EngineTab: FunctionComponent<{
  state: TranslationState;
  installedEnginePlugins: ReadonlySet<string> | undefined;
  canTest: boolean;
  /** The engine whose set-up the tab starts on, when the first-run panel named one. */
  startOn: number | null;
}> = ({ state, installedEnginePlugins, canTest, startOn }) => {
  const engines = orderedEngines(state.instances);
  // The engine whose set-up is shown: the one the user opened, else the selected one, else the first.
  const [opened, setOpened] = useState<number | null>(startOn);
  const open =
    engines.find((item) => item.id === opened) ??
    engines.find((item) => item.id === state.engine) ??
    engines[0] ??
    null;
  return (
    <div className={styles.layout}>
      <EnginePanel
        state={state}
        installedEnginePlugins={installedEnginePlugins}
        openId={open?.id ?? null}
        onOpen={setOpened}
      />
      {open ? (
        // Every engine's set-up stays mounted and only the open one shows, so a draft typed for one
        // engine is still there after a look at another.
        <div>
          {engines.map((engine) => (
            <div key={engine.id} hidden={engine.id !== open.id}>
              <EngineSetup
                engine={engine}
                selected={state.engine === engine.id}
                canTest={canTest}
              />
            </div>
          ))}
        </div>
      ) : (
        <div className={styles.quiet}>
          <p className={styles.quietTitle}>No engine is installed</p>
          Install one from the list to set it up here.
        </div>
      )}
    </div>
  );
};

/** Translation: the engine, its back ends, the defaults and what has been translated. */
const TranslationView: FunctionComponent = () => {
  useDocumentTitle(`Translation - ${useAppTitle()}`);
  const {
    data: loaded,
    isLoading,
    error,
    refetch,
    isFetching,
  } = useTranslationState();
  const {
    state: data,
    waiting,
    installedEnginePlugins,
  } = useRestartAware(loaded);
  // The server says what it can do beyond the settings. Nothing is asked of, or shown for, a route it
  // does not list.
  const can = (capability: TranslationCapability) =>
    data?.capabilities?.includes(capability) === true;
  const summary = useTranslationSummary(can("summary"));
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab") as TabKey | null;
  const tab: TabKey =
    requested && TABS.includes(requested) ? requested : "engine";
  // Once the full page has been shown it stays: by choice ("Set it up by hand"), or because there was
  // something to show. Picking None there does not swap the page for the first-run panel.
  const [full, setFull] = useState(false);
  const [startOn, setStartOn] = useState<number | null>(null);
  // A start that is under way keeps the first-run panel up until it has gone through, whatever a refresh
  // in between says: the panel holds the reason a step failed and the way to try again.
  const [starting, setStarting] = useState(false);
  // Activity is loaded when it is first opened. After that every panel stays mounted, so a draft typed
  // on one tab is still there after a look at another.
  const [activitySeen, setActivitySeen] = useState(tab === "activity");

  if (isLoading && !data) {
    return (
      <Container fluid px="md" className={styles.page}>
        <LoadingPage />
      </Container>
    );
  }
  // Only a page that has never had data is replaced by the error. Once there is data, a failed refresh is
  // shown above it, so the forms (and what is typed in them) stay where they are.
  if (!data) {
    return (
      <Container fluid px="md" className={styles.page}>
        <Alert color="red" role="alert" title="Translation could not be loaded">
          Bazarr+ did not answer. Check that it is running, then try again.
          <div>
            <Button
              mt="sm"
              size="xs"
              variant="default"
              loading={isFetching}
              onClick={() => void refetch()}
            >
              Try again
            </Button>
          </div>
        </Alert>
      </Container>
    );
  }

  const refreshFailed = error ? (
    <Alert color="red" role="alert" mb="md">
      The page could not be refreshed. What is shown may be out of date, and
      nothing you typed has been lost.
    </Alert>
  ) : null;

  if (!full && (isFirstRun(data) || starting)) {
    return (
      <Container fluid px="md" className={styles.page}>
        {refreshFailed}
        <RestartBanner count={waiting} />
        <QuickStart
          state={data}
          onByHand={(engineId) => {
            setStartOn(engineId ?? null);
            setFull(true);
          }}
          onPin={setStarting}
        />
      </Container>
    );
  }
  if (!full) {
    // Latched during this draw, so the full page is what the next one starts from.
    setFull(true);
  }
  const showActivity = activitySeen || tab === "activity";

  return (
    <Container fluid px="md" className={styles.page}>
      {refreshFailed}
      <RestartBanner count={waiting} />
      <StatusBand state={data} summary={summary.data} />
      <Tabs
        value={tab}
        onChange={(next) => {
          if (!next) {
            return;
          }
          const updated = new URLSearchParams(params);
          if (next === "engine") {
            updated.delete("tab");
          } else {
            updated.set("tab", next);
          }
          if (next === "activity") {
            setActivitySeen(true);
          }
          setParams(updated, { replace: true });
        }}
      >
        <Tabs.List>
          <Tabs.Tab
            value="engine"
            leftSection={<FontAwesomeIcon icon={faLanguage} />}
          >
            Engine
          </Tabs.Tab>
          <Tabs.Tab
            value="defaults"
            leftSection={<FontAwesomeIcon icon={faSliders} />}
          >
            Defaults
          </Tabs.Tab>
          <Tabs.Tab
            value="activity"
            leftSection={<FontAwesomeIcon icon={faClockRotateLeft} />}
          >
            Activity
          </Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="engine" pt="lg">
          <EngineTab
            state={data}
            installedEnginePlugins={installedEnginePlugins}
            canTest={can("test")}
            startOn={startOn}
          />
        </Tabs.Panel>
        <Tabs.Panel value="defaults" pt="lg">
          <DefaultsPanel summary={can("limits") ? summary.data : undefined} />
        </Tabs.Panel>
        <Tabs.Panel value="activity" pt="lg">
          {showActivity && (
            <ActivityPanel
              state={data}
              active={tab === "activity"}
              canQueue={can("queue")}
            />
          )}
        </Tabs.Panel>
      </Tabs>
    </Container>
  );
};

export default TranslationView;
