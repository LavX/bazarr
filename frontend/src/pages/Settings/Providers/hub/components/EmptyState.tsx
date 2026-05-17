import { FunctionComponent, ReactNode } from "react";
import { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface EmptyStateProps {
  icon: IconDefinition;
  title: string;
  body?: string;
  action?: ReactNode;
}

export const EmptyState: FunctionComponent<EmptyStateProps> = ({
  icon,
  title,
  body,
  action,
}) => (
  <div className={styles.emptyState}>
    <div className={styles.emptyStateIconTile}>
      <FontAwesomeIcon icon={icon} />
    </div>
    <div className={styles.emptyStateTitle}>{title}</div>
    {body && <div className={styles.emptyStateBody}>{body}</div>}
    {action}
  </div>
);
