import { FunctionComponent } from "react";
import { Tooltip } from "@mantine/core";
import { faCloud } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

/**
 * Indicates that a provider can use FlareSolverr to clear Cloudflare browser
 * challenges. This is a Cloudflare bypass, distinct from an anti-captcha
 * service, so it gets its own badge.
 * See https://github.com/LavX/bazarr/issues/215
 */
export const FlareSolverrBadge: FunctionComponent = () => {
  return (
    <Tooltip label="Can use FlareSolverr to clear Cloudflare challenges">
      <span
        className={clsx(styles.pill, styles["tone-info"])}
        role="status"
        aria-label="Can use FlareSolverr"
      >
        <FontAwesomeIcon icon={faCloud} className={styles.pillIcon} />
        <span>FlareSolverr</span>
      </span>
    </Tooltip>
  );
};
