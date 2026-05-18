import { FunctionComponent } from "react";
import { Button, Group } from "@mantine/core";
import {
  faArrowRight,
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
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface UpdateCardProps {
  provider: ProviderHubInstallation;
  latest: ProviderHubCatalogEntry;
  onApply: (id: string) => void;
  isApplying?: boolean;
}

export const UpdateCard: FunctionComponent<UpdateCardProps> = ({
  provider,
  latest,
  onApply,
  isApplying,
}) => (
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
          <div
            className={styles.hubCardMeta}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginTop: 2,
            }}
          >
            <span>v{provider.active_version ?? "?"}</span>
            <FontAwesomeIcon icon={faArrowRight} style={{ fontSize: 10 }} />
            <span style={{ color: "var(--bz-text-primary)", fontWeight: 600 }}>
              v{latest.version}
            </span>
          </div>
        </div>
      </div>
    </div>
    <div className={styles.hubCardFooter}>
      <Group gap={6} className={styles.hubCardPills}>
        <TrustBadge trusted={latest.trusted} />
      </Group>
      {provider.pending_restart ? (
        <span className={styles.hubCardMeta}>Staged</span>
      ) : (
        <Button
          size="xs"
          variant="light"
          loading={isApplying}
          onClick={() => onApply(provider.provider_id)}
          leftSection={<FontAwesomeIcon icon={faRotate} />}
        >
          Apply update
        </Button>
      )}
    </div>
  </div>
);
