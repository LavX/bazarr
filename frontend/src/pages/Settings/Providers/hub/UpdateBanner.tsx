import { FunctionComponent } from "react";
import { Button } from "@mantine/core";
import {
  faCircleArrowUp,
  faClock,
  faPowerOff,
  faXmark,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import { useSystem } from "@/apis/hooks";
import type {
  ProviderHubCatalog,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { summarizeUpdates } from "@/pages/Settings/Providers/hub/utils";
import { markRestartReloadPending } from "@/utilities/restart";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface UpdateBannerProps {
  providers: ProviderHubInstallation[] | undefined;
  catalog: ProviderHubCatalog | undefined | null;
  onSwitchToUpdates: () => void;
  onDismiss?: () => void;
}

export const UpdateBanner: FunctionComponent<UpdateBannerProps> = ({
  providers,
  catalog,
  onSwitchToUpdates,
  onDismiss,
}) => {
  const { restart, isMutating } = useSystem();
  const summary = summarizeUpdates(providers, catalog);
  const hasUpdates = summary.available.length > 0;
  const hasRestart = summary.pendingRestart.length > 0;

  const handleRestart = () => {
    markRestartReloadPending();
    restart();
  };

  if (!hasUpdates && !hasRestart) return null;

  if (hasRestart) {
    const n = summary.pendingRestart.length;
    return (
      <div
        className={clsx(styles.banner, styles["banner-warning"])}
        role="alert"
      >
        <FontAwesomeIcon icon={faClock} className={styles.bannerIcon} />
        <div className={styles.bannerBody}>
          <div className={styles.bannerTitle}>
            {n === 1
              ? "1 provider is staged. Restart Bazarr+ to activate."
              : `${n} providers are staged. Restart Bazarr+ to activate.`}
          </div>
          <div className={styles.bannerSubtitle}>
            Staged versions sit alongside the active version until the next
            restart.
          </div>
        </div>
        <div className={styles.bannerActions}>
          <Button
            size="xs"
            variant="light"
            leftSection={<FontAwesomeIcon icon={faPowerOff} />}
            loading={isMutating}
            onClick={handleRestart}
          >
            Restart Bazarr now
          </Button>
        </div>
      </div>
    );
  }

  const n = summary.available.length;
  return (
    <div className={clsx(styles.banner, styles["banner-info"])} role="status">
      <FontAwesomeIcon icon={faCircleArrowUp} className={styles.bannerIcon} />
      <div className={styles.bannerBody}>
        <div className={styles.bannerTitle}>
          {n === 1
            ? "1 provider update is available"
            : `${n} provider updates are available`}
        </div>
        <div className={styles.bannerSubtitle}>
          Review changes before applying. A restart is required to activate
          staged versions.
        </div>
      </div>
      <div className={styles.bannerActions}>
        <Button size="xs" variant="light" onClick={onSwitchToUpdates}>
          Review updates
        </Button>
      </div>
      {onDismiss && (
        <button
          type="button"
          className={styles.bannerDismiss}
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          <FontAwesomeIcon icon={faXmark} />
        </button>
      )}
    </div>
  );
};
