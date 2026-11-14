import { FunctionComponent } from "react";
import { Button, Group } from "@mantine/core";
import styles from "./Translation.module.scss";

export interface ConflictNoticeProps {
  /** What changed, in a few words: "This back end", "These settings". */
  subject: string;
  busy?: boolean;
  onSaveMine: () => void;
  onDiscard: () => void;
}

/**
 * A save was refused because the row was changed somewhere else since the form
 * opened. What was typed is still in the form; the two ways out are spelled
 * out, so overwriting is a choice and never a second click on Save.
 */
const ConflictNotice: FunctionComponent<ConflictNoticeProps> = ({
  subject,
  busy,
  onSaveMine,
  onDiscard,
}) => (
  <div className={styles.conflict} role="alert">
    <p className={styles.conflictTitle}>
      {subject} changed somewhere else since you opened this form
    </p>
    <p className={styles.conflictText}>
      Nothing you typed is lost. Save your version over the other change, or
      discard yours and load what is saved now.
    </p>
    <Group gap="xs">
      <Button size="xs" loading={busy} onClick={onSaveMine}>
        Save my version
      </Button>
      <Button size="xs" variant="default" disabled={busy} onClick={onDiscard}>
        Discard and reload
      </Button>
    </Group>
  </div>
);

export default ConflictNotice;
