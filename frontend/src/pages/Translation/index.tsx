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
import type { TranslationState } from "@/types/translation";
import ActivityPanel from "./ActivityPanel";
import DefaultsPanel from "./DefaultsPanel";
import EnginePanel from "./EnginePanel";
import EngineSetup from "./EngineSetup";
import { isFirstRun, orderedEngines } from "./model";
import QuickStart from "./QuickStart";
import StatusBand from "./StatusBand";
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

const EngineTab: FunctionComponent<{ state: TranslationState }> = ({
  state,
}) => {
  const engines = orderedEngines(state.instances);
  // The engine whose set-up is shown: the one the user opened, else the selected one, else the first.
  const [opened, setOpened] = useState<number | null>(null);
  const open =
    engines.find((item) => item.id === opened) ??
    engines.find((item) => item.id === state.engine) ??
    engines[0] ??
    null;
  return (
    <div className={styles.layout}>
      <EnginePanel state={state} openId={open?.id ?? null} onOpen={setOpened} />
      {open ? (
        <EngineSetup
          key={open.id}
          engine={open}
          selected={state.engine === open.id}
        />
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
  const { data, isLoading, error, refetch, isFetching } = useTranslationState();
  const summary = useTranslationSummary();
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab") as TabKey | null;
  const tab: TabKey =
    requested && TABS.includes(requested) ? requested : "engine";
  // First run can be left for the full page; the choice lasts while the page is open.
  const [byHand, setByHand] = useState(false);

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

  if (isFirstRun(data) && !byHand) {
    return (
      <Container fluid px="md" className={styles.page}>
        {refreshFailed}
        <QuickStart state={data} onByHand={() => setByHand(true)} />
      </Container>
    );
  }

  return (
    <Container fluid px="md" className={styles.page}>
      {refreshFailed}
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
          setParams(updated, { replace: true });
        }}
        keepMounted={false}
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
          <EngineTab state={data} />
        </Tabs.Panel>
        <Tabs.Panel value="defaults" pt="lg">
          <DefaultsPanel summary={summary.data} />
        </Tabs.Panel>
        <Tabs.Panel value="activity" pt="lg">
          <ActivityPanel state={data} />
        </Tabs.Panel>
      </Tabs>
    </Container>
  );
};

export default TranslationView;
