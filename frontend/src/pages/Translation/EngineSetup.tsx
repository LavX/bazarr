import { FunctionComponent } from "react";
import type { TranslationEngine } from "@/types/translation";
import BackendList from "./BackendList";
import EngineSettings from "./EngineSettings";
import { engineName, engineStatus, hasBackends } from "./model";
import styles from "./Translation.module.scss";

/** One engine's set-up: its back ends in order, then its own settings, both drawn from its schema. */
const EngineSetup: FunctionComponent<{
  engine: TranslationEngine;
  selected: boolean;
}> = ({ engine, selected }) => {
  const status = engineStatus(engine);
  const fields = engine.schema?.fields ?? [];
  const backends = hasBackends(engine);
  return (
    <div>
      <h2 className={styles.setupTitle}>{engineName(engine)}</h2>
      <p className={styles.sectionHint}>
        {status.next ||
          (selected
            ? "This engine translates your subtitles."
            : "Set up and ready. Pick it under Engine to translate with it.")}
      </p>

      {backends && (
        <section className={styles.section} style={{ marginTop: 24 }}>
          <div className={styles.sectionHead}>
            <div>
              <h3 className={styles.sectionTitle}>Back ends</h3>
              <p className={styles.sectionHint}>
                Tried from the top. A job moves to the next one only when a back
                end is rate limited, out of quota, signed out or unreachable.
              </p>
            </div>
          </div>
          <BackendList engine={engine} />
        </section>
      )}

      {fields.length > 0 && (
        <section className={styles.section}>
          <div className={styles.sectionHead}>
            <h3 className={styles.sectionTitle}>Engine settings</h3>
          </div>
          <EngineSettings key={engine.id} engine={engine} />
        </section>
      )}

      {!backends && fields.length === 0 && engine.plugin?.active && (
        <div className={styles.quiet} style={{ marginTop: 24 }}>
          <p className={styles.quietTitle}>Nothing to set up</p>
          This engine works as it is.
        </div>
      )}
    </div>
  );
};

export default EngineSetup;
