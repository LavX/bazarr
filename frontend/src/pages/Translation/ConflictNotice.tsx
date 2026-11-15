import { FunctionComponent } from "react";
import { Button, Group } from "@mantine/core";
import styles from "./Translation.module.scss";

export interface ConflictNoticeProps {
  /** What changed, in a few words: "This back end", "These settings". */
  subject: string;
  /** The save over the other change is on its way. */
  saving?: boolean;
  /** What is saved now is being loaded, for the reload. */
  reloading?: boolean;
  /** Why what is saved now could not be loaded. */
  problem?: string;
  /** Something else on the form is on its way: neither answer can be given yet. */
  disabled?: boolean;
  onSaveMine: () => void;
  onDiscard: () => void;
}

/**
 * A save was refused because the row was changed somewhere else since the form
 * opened. What was typed is still in the form; the two ways out are spelled
 * out, so overwriting is a choice and never a second click on Save. Both load
 * what is saved now first, and neither changes the form until that has
 * answered.
 */
const ConflictNotice: FunctionComponent<ConflictNoticeProps> = ({
  subject,
  saving,
  reloading,
  problem,
  disabled,
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
    {problem && <p className={styles.conflictProblem}>{problem}</p>}
    <Group gap="xs">
      <Button
        size="xs"
        loading={saving}
        disabled={reloading || disabled}
        onClick={onSaveMine}
      >
        Save my version
      </Button>
      <Button
        size="xs"
        variant="default"
        loading={reloading}
        disabled={saving || disabled}
        onClick={onDiscard}
      >
        Discard and reload
      </Button>
    </Group>
  </div>
);

export default ConflictNotice;
