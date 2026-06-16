import { FunctionComponent } from "react";
import { Tooltip } from "@mantine/core";
import { faRobot } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

/**
 * Warns that a provider relies on an Anti-Captcha service or FlareSolverr to
 * solve captchas, so users can decide before installing it.
 * See https://github.com/LavX/bazarr/issues/215
 */
export const AntiCaptchaBadge: FunctionComponent = () => {
  return (
    <Tooltip label="May require an Anti-Captcha service or FlareSolverr to solve captchas">
      <span
        className={clsx(styles.pill, styles["tone-warning"])}
        role="status"
        aria-label="May require an Anti-Captcha service"
      >
        <FontAwesomeIcon icon={faRobot} className={styles.pillIcon} />
        <span>Anti-Captcha</span>
      </span>
    </Tooltip>
  );
};
