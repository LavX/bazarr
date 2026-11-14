/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useId, useState } from "react";
import { Link } from "react-router";
import { Alert, Button, Select } from "@mantine/core";
import { faArrowUp, faCircleCheck } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { readRefusal, usePointersMutation } from "@/apis/hooks/translation";
import { StatusBadge } from "@/pages/Settings/Providers/hub/components/StatusBadge";
import type {
  TranslationAvailableEngine,
  TranslationEngine,
  TranslationState,
} from "@/types/translation";
import InstallConsent from "./InstallConsent";
import {
  backendLabel,
  engineName,
  engineStatus,
  hasBackends,
  orderedEngines,
  working,
} from "./model";
import styles from "./Translation.module.scss";

const NONE = "none";

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
    return "Needs no set-up.";
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
  const pointers = usePointersMutation();
  const [installing, setInstalling] =
    useState<TranslationAvailableEngine | null>(null);
  const engines = orderedEngines(state.instances);
  const available = [...(state.available_engines ?? [])].sort(
    (a, b) => Number(b.official ?? false) - Number(a.official ?? false),
  );
  // The choice shows at once; the saved one replaces it when the save comes back.
  const sent = pointers.isPending ? pointers.variables : undefined;
  const chosen =
    sent && "engine" in sent ? (sent.engine ?? null) : state.engine;
  const fallback =
    sent && "fallback_engine" in sent
      ? (sent.fallback_engine ?? null)
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
      >
        <input
          type="radio"
          className={styles.engineRadio}
          id={id}
          name={group}
          checked={chosen === engine.id}
          aria-describedby={`${id}-text`}
          onChange={() => pointers.mutate({ engine: engine.id })}
        />
        <div className={styles.engineBody}>
          <label className={styles.engineLabel} htmlFor={id}>
            {engineName(engine)}
            {engine.official && <OfficialMark />}
          </label>
          <p className={styles.engineText} id={`${id}-text`}>
            {engineLine(engine)}
          </p>
          <div className={styles.engineMeta}>
            <StatusBadge tone={status.tone} label={status.label} />
            {engine.plugin?.version && (
              <span className={styles.version}>v{engine.plugin.version}</span>
            )}
            {engine.plugin?.update_version && (
              <Link
                className={styles.chip}
                to="/subtitle-hub?tab=updates"
                title={`Version ${engine.plugin.update_version} is available. Updates are installed in the Subtitle Hub.`}
              >
                <FontAwesomeIcon icon={faArrowUp} />
                Update available
              </Link>
            )}
            {openId !== engine.id && (
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
            <Button
              size="compact-sm"
              variant="light"
              aria-label={`Install ${engine.name}`}
              onClick={() => setInstalling(engine)}
            >
              Install
            </Button>
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
            onChange={() => pointers.mutate({ engine: null })}
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
      {(pointers.data?.notices ?? []).map((notice) => (
        <Alert
          key={notice.code + String(notice.instance_id)}
          color="yellow"
          mt="sm"
        >
          {notice.message}
        </Alert>
      ))}
      {pointers.error && (
        <Alert color="red" role="alert" mt="sm">
          {readRefusal(pointers.error)?.message ??
            "The choice could not be saved. Try again."}
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
        onChange={(next) => pointers.mutate({ fallback_engine: toId(next) })}
      />
      <InstallConsent engine={installing} onClose={() => setInstalling(null)} />
    </section>
  );
};

export default EnginePanel;
