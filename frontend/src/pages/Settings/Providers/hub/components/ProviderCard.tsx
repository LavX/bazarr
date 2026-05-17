import { FunctionComponent } from "react";
import { ActionIcon, Group, Menu, Tooltip } from "@mantine/core";
import {
  faEllipsis,
  faPuzzlePiece,
  faTrash,
  faVial,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import type { ProviderHubInstallation } from "@/apis/raw/providerHub";
import { ProviderStatusBadge } from "@/pages/Settings/Providers/hub/components/StatusBadge";
import { TrustBadge } from "@/pages/Settings/Providers/hub/components/TrustBadge";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface ProviderCardProps {
  provider: ProviderHubInstallation;
  onTest: (id: string) => void;
  onUninstall: (id: string) => void;
  isTesting?: boolean;
}

export const ProviderCard: FunctionComponent<ProviderCardProps> = ({
  provider,
  onTest,
  onUninstall,
  isTesting,
}) => {
  const versionLabel =
    provider.active_version ?? provider.staged_version ?? "no version";

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
              {provider.name ?? provider.provider_id}
            </div>
            <div className={styles.hubCardMeta}>v{versionLabel}</div>
          </div>
        </div>
        <Menu position="bottom-end" withinPortal>
          <Menu.Target>
            <ActionIcon
              variant="subtle"
              color="gray"
              aria-label="Provider actions"
            >
              <FontAwesomeIcon icon={faEllipsis} />
            </ActionIcon>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item
              leftSection={<FontAwesomeIcon icon={faVial} />}
              onClick={() => onTest(provider.provider_id)}
              disabled={isTesting}
            >
              Test connection
            </Menu.Item>
            <Menu.Divider />
            <Menu.Item
              color="red"
              leftSection={<FontAwesomeIcon icon={faTrash} />}
              onClick={() => onUninstall(provider.provider_id)}
            >
              Uninstall
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </div>
      {provider.last_error && (
        <Tooltip label={provider.last_error} multiline w={300}>
          <div className={styles.hubCardDescription}>
            Last error: {provider.last_error}
          </div>
        </Tooltip>
      )}
      <div className={styles.hubCardFooter}>
        <Group gap={6} className={styles.hubCardPills}>
          <ProviderStatusBadge
            state={provider.state}
            pendingRestart={provider.pending_restart}
          />
          <TrustBadge trusted={provider.trusted} />
        </Group>
      </div>
    </div>
  );
};
