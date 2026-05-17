import { FunctionComponent } from "react";
import { Tooltip } from "@mantine/core";
import { faShieldHalved, faUsers } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface TrustBadgeProps {
  trusted: boolean | undefined;
}

export const TrustBadge: FunctionComponent<TrustBadgeProps> = ({ trusted }) => {
  if (trusted) {
    return (
      <Tooltip label="From a trusted catalog source">
        <span
          className={clsx(styles.pill, styles["tone-info"])}
          role="status"
          aria-label="Trusted source"
        >
          <FontAwesomeIcon icon={faShieldHalved} className={styles.pillIcon} />
          <span>Trusted</span>
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip label="From a community catalog source, review before installing">
      <span
        className={clsx(styles.pill, styles["tone-neutral"])}
        role="status"
        aria-label="Community source"
      >
        <FontAwesomeIcon icon={faUsers} className={styles.pillIcon} />
        <span>Community</span>
      </span>
    </Tooltip>
  );
};
