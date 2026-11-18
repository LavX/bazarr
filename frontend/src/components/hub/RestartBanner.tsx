import { FunctionComponent } from "react";
import { Button } from "@mantine/core";
import { faClock, faPowerOff } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import { useSystem } from "@/apis/hooks";
import { markRestartReloadPending } from "@/utilities/restart";
import styles from "@/components/hub/hub.module.scss";

/**
 * Says how many changes wait on a restart and offers the restart. The count is of the entries whose
 * `needs_restart` the server set, so the banner is there for exactly as long as the server says a
 * restart is needed, and a kind that attaches live never raises it.
 */
export const RestartBanner: FunctionComponent<{ count: number }> = ({
  count,
}) => {
  const { restart, isMutating } = useSystem();
  if (count === 0) return null;
  return (
    <div className={clsx(styles.banner, styles["banner-warning"])} role="alert">
      <FontAwesomeIcon icon={faClock} className={styles.bannerIcon} />
      <div className={styles.bannerBody}>
        <div className={styles.bannerTitle}>
          {count === 1
            ? "1 plugin change is waiting for a restart."
            : `${count} plugin changes are waiting for a restart.`}
        </div>
        <div className={styles.bannerSubtitle}>
          Installs, updates and removals of these plugins finish when Bazarr+
          restarts.
        </div>
      </div>
      <div className={styles.bannerActions}>
        <Button
          size="xs"
          variant="light"
          leftSection={<FontAwesomeIcon icon={faPowerOff} />}
          loading={isMutating}
          onClick={() => {
            markRestartReloadPending();
            restart();
          }}
        >
          Restart Bazarr now
        </Button>
      </div>
    </div>
  );
};
