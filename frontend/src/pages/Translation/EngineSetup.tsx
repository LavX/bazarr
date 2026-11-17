import { FunctionComponent, useState } from "react";
import { Alert, Switch } from "@mantine/core";
import type { TranslationEngine } from "@/types/translation";
import AttentionNotice from "./AttentionNotice";
import BackendList from "./BackendList";
import EngineSettings from "./EngineSettings";
import {
  canRun,
  engineName,
  engineStatus,
  hasBackends,
  heldBack,
} from "./model";
import { useDraftRevision } from "./useDraftRevision";
import { useEngineSwitch } from "./useEngineSwitch";
import styles from "./Translation.module.scss";

/**
 * One engine's set-up: its name with its on and off switch, then its back ends in order and its own
 * settings, both drawn from its schema.
 *
 * The switch is beside the name for every engine, whatever it has to set up: an engine that was
 * turned off, by hand or by a plugin update, is turned on again in the same place. It saves at once,
 * like the switch on a back end's row. Why the engine is off is said right under its name.
 */
const EngineSetup: FunctionComponent<{
  engine: TranslationEngine;
  selected: boolean;
  /** Whether the server can send a test request through a back end. */
  canTest?: boolean;
}> = ({ engine, selected, canTest }) => {
  const status = engineStatus(engine);
  const name = engineName(engine);
  const fields = engine.schema?.fields ?? [];
  const backends = hasBackends(engine);
  const revision = useDraftRevision(engine.revision);
  const power = useEngineSwitch(engine, revision);
  const [formBusy, setFormBusy] = useState(false);
  // Flagged for a look, and nothing more pressing (a plugin that is not running) to say first.
  const held = heldBack(engine) && canRun(engine);
  return (
    <div>
      <div className={styles.setupHead}>
        <h2 className={styles.setupTitle}>{name}</h2>
        <Switch
          classNames={{ label: styles.powerLabel }}
          // The word beside it says where it stands; the name says what it does, whichever way it is.
          label={power.checked ? "On" : "Off"}
          aria-label={`Turn ${name} on or off`}
          checked={power.checked}
          aria-busy={power.busy}
          // The settings form writes the same row: while it saves, the switch waits its turn.
          disabled={formBusy}
          onChange={(event) => void power.set(event.currentTarget.checked)}
        />
      </div>
      {held ? (
        <AttentionNotice
          title={
            fields.length > 0
              ? "Turned off until its settings are checked"
              : "This engine was turned off"
          }
        >
          {fields.length > 0
            ? "What it has saved no longer fits, usually because a plugin update changed what the engine needs. Check the engine settings below and save them, then turn it on with the switch beside its name."
            : "A plugin update usually does this. Turn it on with the switch beside its name."}
        </AttentionNotice>
      ) : (
        <p className={styles.sectionHint}>
          {status.next ||
            (selected
              ? "This engine translates your subtitles."
              : "Set up and ready. Pick it under Engine to translate with it.")}
        </p>
      )}
      {power.problem && (
        <Alert color="red" role="alert" mt="sm" className={styles.setupNote}>
          {power.problem}
        </Alert>
      )}
      {power.notices.map((notice) => (
        <Alert
          key={notice.code}
          color="yellow"
          mt="sm"
          className={styles.setupNote}
        >
          {notice.message}
        </Alert>
      ))}

      <div className={styles.setupBody}>
        {backends && (
          <section className={styles.section}>
            <div className={styles.sectionHead}>
              <div>
                <h3 className={styles.sectionTitle}>Back ends</h3>
                <p className={styles.sectionHint}>
                  Tried from the top. A job moves to the next one only when a
                  back end is rate limited, out of quota, signed out or
                  unreachable.
                </p>
              </div>
            </div>
            <BackendList engine={engine} canTest={canTest} />
          </section>
        )}

        {fields.length > 0 && (
          <section className={styles.section}>
            <div className={styles.sectionHead}>
              <h3 className={styles.sectionTitle}>Engine settings</h3>
            </div>
            <EngineSettings
              key={engine.id}
              engine={engine}
              revision={revision}
              switching={power.busy}
              switchErrors={power.fieldErrors}
              onBusy={setFormBusy}
              onSaved={power.dismiss}
            />
          </section>
        )}

        {!backends && fields.length === 0 && canRun(engine) && (
          <div className={styles.quiet}>
            <p className={styles.quietTitle}>Nothing to set up</p>
            {engine.enabled
              ? "This engine works as it is."
              : "This engine has no settings. Once it is on, it works as it is."}
          </div>
        )}
      </div>
    </div>
  );
};

export default EngineSetup;
