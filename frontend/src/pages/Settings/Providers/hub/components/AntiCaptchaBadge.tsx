import { FunctionComponent } from "react";
import { Tooltip } from "@mantine/core";
import { faRobot } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

/**
 * Warns that a provider needs an external anti-captcha service to solve
 * captchas (for example reCAPTCHA or image verification), so users can decide
 * before installing it. FlareSolverr (a Cloudflare bypass) is surfaced
 * separately via FlareSolverrBadge.
 * See https://github.com/LavX/bazarr/issues/215
 */
export const AntiCaptchaBadge: FunctionComponent = () => {
  return (
    <Tooltip label="Needs an external anti-captcha service to solve captchas">
      <span
        className={clsx(styles.pill, styles["tone-warning"])}
        role="status"
        aria-label="Needs an anti-captcha service"
      >
        <FontAwesomeIcon icon={faRobot} className={styles.pillIcon} />
        <span>Anti-Captcha</span>
      </span>
    </Tooltip>
  );
};
