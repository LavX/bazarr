/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useId, useRef, useState } from "react";
import { Link } from "react-router";
import { Alert, Button, Select } from "@mantine/core";
import { faArrowUp, faCircleCheck } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  afterStateDelivered,
  readRefusal,
  usePointersMutation,
} from "@/apis/hooks/translation";
import { StatusBadge } from "@/components/hub/StatusBadge";
import type {
  TranslationAvailableEngine,
  TranslationEngine,
  TranslationNotice,
  TranslationState,
} from "@/types/translation";
import {
  backendLabel,
  engineName,
  engineStatus,
  hasBackends,
  orderedEngines,
  working,
} from "./model";
import { useEngineInstall } from "./useEngineInstall";
import styles from "./Translation.module.scss";

const NONE = "none";

interface PointerChoice {
  engine?: number | null;
  fallback_engine?: number | null;
}

/**
 * A choice laid over the ones before it. The engine and the fallback are never the same engine: when
 * the engine picked now is the fallback that was picked a moment ago, that fallback is dropped, so
 * the two never go out together as one request the server has to refuse.
 */
function together(before: PointerChoice, change: PointerChoice): PointerChoice {
  const merged = { ...before, ...change };
  if (
    "engine" in change &&
    change.engine !== null &&
    merged.fallback_engine === change.engine
  ) {
    delete merged.fallback_engine;
  }
  return merged;
}

/**
 * The engine and fallback choices, written one request at a time. A choice made while a write is on
 * its way waits, and replaces any other that was waiting: requests never overlap, so they cannot land
 * out of order, and the last choice is the one that ends up saved. The choice shows at once.
 */
function usePointerChoices() {
  const pointers = usePointersMutation();
  // What was chosen and is not confirmed yet.
  const [chosen, setChosen] = useState<PointerChoice>({});
  const [notices, setNotices] = useState<TranslationNotice[]>([]);
  const [problem, setProblem] = useState("");
  const waiting = useRef<PointerChoice | null>(null);
  const writing = useRef(false);

  const write = async () => {
    if (writing.current) {
      return;
    }
    writing.current = true;
    try {
      while (waiting.current) {
        const next = waiting.current;
        waiting.current = null;
        try {
          const answer = await pointers.mutateAsync(next);
          if (!waiting.current) {
            setNotices(answer.notices ?? []);
          }
        } catch (error) {
          // What is saved stands; a choice made meanwhile was made on top of the refused one.
          waiting.current = null;
          setProblem(
            readRefusal(error)?.message ||
              "The choice could not be saved. Try again.",
          );
        }
      }
    } finally {
      writing.current = false;
      afterStateDelivered(() => {
        if (!writing.current) {
          setChosen({});
        }
      });
    }
  };

  return {
    chosen,
    notices,
    problem,
    choose: (change: PointerChoice) => {
      setProblem("");
      setChosen((current) => together(current, change));
      waiting.current = together(waiting.current ?? {}, change);
      void write();
    },
  };
}

/** What "Legacy" means, said in words wherever the mark is shown. */
export const LEGACY_NOTE =
  "Kept from earlier versions: it is not a plugin, so there is nothing to install or update.";

/** For an engine kept from earlier versions, which Bazarr+ runs itself. */
export const LegacyMark: FunctionComponent = () => (
  <span className={styles.legacy}>Legacy</span>
);

export const OfficialMark: FunctionComponent = () => (
  <span className={styles.official}>
    <FontAwesomeIcon icon={faCircleCheck} />
    Official
  </span>
);

/** One line under an engine's name: what it will do, or what it still needs. */
function engineLine(engine: TranslationEngine): string {
  const status = engineStatus(engine);
  if (status.next) {
    return status.next;
  }
  if (!hasBackends(engine)) {
    return engine.description || "Needs no set-up.";
  }
  const on = engine.connections.filter(working);
  const first = backendLabel(engine, on[0]);
  return on.length === 1
    ? `Translates with ${first}.`
    : `Tries ${first} first, then ${on.length - 1} more.`;
}

export interface EnginePanelProps {
  state: TranslationState;
  /** The engine whose set-up is shown beside the list. */
  openId: number | null;
  onOpen: (id: number) => void;
}

/** Which engine translates, which one takes over when it declines, and what can be installed. */
const EnginePanel: FunctionComponent<EnginePanelProps> = ({
  state,
  openId,
  onOpen,
}) => {
  const group = useId();
  const pointers = usePointerChoices();
  const install = useEngineInstall(state);
  const engines = orderedEngines(state.instances);
  const available = [...(state.available_engines ?? [])].sort(
    (a, b) => Number(b.official ?? false) - Number(a.official ?? false),
  );
  // The choice shows at once; the saved one replaces it when the save comes back.
  const chosen =
    "engine" in pointers.chosen
      ? (pointers.chosen.engine ?? null)
      : state.engine;
  const fallback =
    "fallback_engine" in pointers.chosen
      ? (pointers.chosen.fallback_engine ?? null)
      : state.fallback_engine;
  const toId = (value: string | null) =>
    value === null || value === NONE ? null : Number(value);

  const installed = (engine: TranslationEngine) => {
    const status = engineStatus(engine);
    const id = `${group}-${engine.id}`;
    return (
      <div
        key={engine.id}
        className={styles.engineCard}
        data-checked={chosen === engine.id}
        data-unavailable={engine.removing ? "true" : undefined}
      >
        <input
          type="radio"
          className={styles.engineRadio}
          id={id}
          name={group}
          checked={chosen === engine.id}
          disabled={engine.removing}
          aria-describedby={`${id}-text`}
          onChange={() => pointers.choose({ engine: engine.id })}
        />
        <div className={styles.engineBody}>
          <label className={styles.engineLabel} htmlFor={id}>
            {engineName(engine)}
            {engine.official && <OfficialMark />}
            {engine.legacy && <LegacyMark />}
          </label>
          <p className={styles.engineText} id={`${id}-text`}>
            {engineLine(engine)}
            {engine.legacy && ` ${LEGACY_NOTE}`}
          </p>
          <div className={styles.engineMeta}>
            <StatusBadge tone={status.tone} label={status.label} />
            {engine.plugin?.version && (
              <span className={styles.version}>v{engine.plugin.version}</span>
            )}
            {engine.plugin?.update_version && (
              <Link
                className={styles.chip}
                to="/plugin-hub?tab=updates"
                title={`Version ${engine.plugin.update_version} is available. Updates are installed in the Plugin Hub.`}
              >
                <FontAwesomeIcon icon={faArrowUp} />
                Update available
              </Link>
            )}
            {openId !== engine.id && !engine.removing && (
              <Button
                size="compact-xs"
                variant="subtle"
                ml="auto"
                aria-label={`Set up ${engineName(engine)}`}
                onClick={() => onOpen(engine.id)}
              >
                Set up
              </Button>
            )}
          </div>
        </div>
      </div>
    );
  };

  const installable = (engine: TranslationAvailableEngine) => {
    const id = `${group}-${engine.plugin_id}`;
    return (
      <div
        key={engine.plugin_id}
        className={styles.engineCard}
        data-unavailable="true"
      >
        <input
          type="radio"
          className={styles.engineRadio}
          id={id}
          name={group}
          disabled
          aria-describedby={`${id}-text`}
        />
        <div className={styles.engineBody}>
          <label className={styles.engineLabel} htmlFor={id}>
            {engine.name}
            {engine.official && <OfficialMark />}
          </label>
          <p className={styles.engineText} id={`${id}-text`}>
            {engine.description ||
              (engine.free
                ? "Free, no key needed."
                : "Not installed yet.")}{" "}
            Install it to use it.
          </p>
          <div className={styles.engineMeta}>
            {engine.awaiting_restart ? (
              <StatusBadge tone="warning" label="Pending restart" />
            ) : (
              <Button
                size="compact-sm"
                variant="light"
                disabled={install.busy}
                aria-label={`Install ${engine.name}`}
                onClick={() => install.begin(engine)}
              >
                Install
              </Button>
            )}
            <span className={styles.version}>v{engine.version}</span>
          </div>
        </div>
      </div>
    );
  };

  const officialAvailable = available.filter((item) => item.official);
  const otherAvailable = available.filter((item) => !item.official);

  return (
    <section className={styles.section} aria-labelledby={`${group}-title`}>
      <div className={styles.sectionHead}>
        <div>
          <h2 className={styles.sectionTitle} id={`${group}-title`}>
            Engine
          </h2>
          <p className={styles.sectionHint}>
            The engine translates a subtitle when none is found in your
            language.
          </p>
        </div>
      </div>
      <div
        className={styles.engines}
        role="radiogroup"
        aria-labelledby={`${group}-title`}
      >
        {officialAvailable.map(installable)}
        {engines.map(installed)}
        {otherAvailable.map(installable)}
        <div className={styles.engineCard} data-checked={chosen === null}>
          <input
            type="radio"
            className={styles.engineRadio}
            id={`${group}-none`}
            name={group}
            checked={chosen === null}
            aria-describedby={`${group}-none-text`}
            onChange={() => pointers.choose({ engine: null })}
          />
          <div className={styles.engineBody}>
            <label className={styles.engineLabel} htmlFor={`${group}-none`}>
              None
            </label>
            <p className={styles.engineText} id={`${group}-none-text`}>
              Translation is off. Subtitles are searched for, never translated.
            </p>
          </div>
        </div>
      </div>
      {pointers.notices.map((notice) => (
        <Alert
          key={notice.code + String(notice.instance_id)}
          color="yellow"
          mt="sm"
        >
          {notice.message}
        </Alert>
      ))}
      {pointers.problem && (
        <Alert color="red" role="alert" mt="sm">
          {pointers.problem}
        </Alert>
      )}
      <Select
        mt="lg"
        label="Fallback engine"
        description="Takes a job the engine declines. Most set-ups need none."
        data={[
          { value: NONE, label: "None" },
          ...engines
            .filter((item) => item.id !== chosen)
            .map((item) => ({
              value: String(item.id),
              label: `${engineName(item)}${item.official ? " (Official)" : ""}`,
            })),
        ]}
        value={fallback === null ? NONE : String(fallback)}
        allowDeselect={false}
        onChange={(next) => pointers.choose({ fallback_engine: toId(next) })}
      />
      {install.dialog}
    </section>
  );
};

export default EnginePanel;
