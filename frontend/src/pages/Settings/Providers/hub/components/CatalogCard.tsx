import { FunctionComponent, useMemo } from "react";
import { Button, Group, Tooltip } from "@mantine/core";
import {
  faCheck,
  faDownload,
  faPuzzlePiece,
  faRotate,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import type {
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { TrustBadge } from "@/pages/Settings/Providers/hub/components/TrustBadge";
import { parseManifest } from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface CatalogCardProps {
  entry: ProviderHubCatalogEntry;
  installed: ProviderHubInstallation | null;
  onInstall: (entry: ProviderHubCatalogEntry) => void;
  isInstalling: boolean;
}

type CtaState = "install" | "installed" | "update" | "restart" | "broken";

function deriveCta(
  entry: ProviderHubCatalogEntry,
  installed: ProviderHubInstallation | null,
  manifestValid: boolean,
): CtaState {
  if (!manifestValid) return "broken";
  if (!installed) return "install";
  if (installed.pending_restart) return "restart";
  if (installed.active_version && installed.active_version !== entry.version) {
    return "update";
  }
  return "installed";
}

export const CatalogCard: FunctionComponent<CatalogCardProps> = ({
  entry,
  installed,
  onInstall,
  isInstalling,
}) => {
  const manifest = useMemo(() => parseManifest(entry), [entry]);
  const cta = deriveCta(entry, installed, manifest !== null);

  const ctaButton = (() => {
    switch (cta) {
      case "installed":
        return (
          <Button
            size="xs"
            variant="light"
            color="green"
            disabled
            leftSection={<FontAwesomeIcon icon={faCheck} />}
          >
            Installed
          </Button>
        );
      case "update":
        return (
          <Button
            size="xs"
            variant="light"
            color="yellow"
            loading={isInstalling}
            onClick={() => onInstall(entry)}
            leftSection={<FontAwesomeIcon icon={faRotate} />}
          >
            Update
          </Button>
        );
      case "restart":
        return (
          <Tooltip label="Restart Bazarr+ to activate the staged version">
            <Button size="xs" variant="light" color="yellow" disabled>
              Restart required
            </Button>
          </Tooltip>
        );
      case "broken":
        return (
          <Tooltip label="This catalog entry is missing a valid manifest">
            <Button size="xs" variant="light" color="gray" disabled>
              Unavailable
            </Button>
          </Tooltip>
        );
      case "install":
      default:
        return (
          <Button
            size="xs"
            variant="light"
            loading={isInstalling}
            onClick={() => onInstall(entry)}
            leftSection={<FontAwesomeIcon icon={faDownload} />}
          >
            Install
          </Button>
        );
    }
  })();

  const sourceLabel = entry.source ?? entry.source_name ?? "Unknown source";
  const description =
    (manifest?.description as string | undefined) ??
    (manifest?.summary as string | undefined) ??
    "";

  return (
    <div className={clsx(styles.hubCard)}>
      <div className={styles.hubCardHeader}>
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            minWidth: 0,
          }}
        >
          <div
            aria-hidden="true"
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              background: "var(--bz-hover-bg)",
              color: "var(--bz-text-secondary)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flex: "0 0 auto",
              fontSize: 16,
            }}
          >
            <FontAwesomeIcon icon={faPuzzlePiece} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div className={styles.hubCardTitle}>
              {entry.name ?? entry.provider_id}
            </div>
            <div className={styles.hubCardMeta}>
              v{entry.version} from {sourceLabel}
            </div>
          </div>
        </div>
      </div>
      {description && (
        <div className={styles.hubCardDescription}>{description}</div>
      )}
      <div className={styles.hubCardFooter}>
        <Group gap={6} className={styles.hubCardPills}>
          <TrustBadge trusted={entry.trusted} />
        </Group>
        {ctaButton}
      </div>
    </div>
  );
};
