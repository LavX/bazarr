import { FunctionComponent, ReactNode } from "react";
import { Group } from "@mantine/core";
import { faPuzzlePiece } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import styles from "@/components/hub/hub.module.scss";

interface HubCardProps {
  name: ReactNode;
  /** The line under the name: version and source. */
  meta: ReactNode;
  /** The actions menu at the top right, when the card has one. */
  menu?: ReactNode;
  description?: ReactNode;
  /** A note about the card's state, read as one by assistive technology. */
  note?: ReactNode;
  /** Anything that sits between the description and the chips. */
  extra?: ReactNode;
  chips?: ReactNode;
  /** The status and trust pills at the bottom left. */
  pills: ReactNode;
  /** The call to action at the bottom right. */
  action: ReactNode;
}

/**
 * The frame every hub card shares, the Subtitle Hub's and the Plugin Hub's: an icon tile, the name and
 * where it comes from, a description, then the status pills and the one action. A card puts its own
 * facts in the slots, so a change to the frame is made once.
 */
export const HubCard: FunctionComponent<HubCardProps> = ({
  name,
  meta,
  menu,
  description,
  note,
  extra,
  chips,
  pills,
  action,
}) => (
  <div className={styles.hubCard}>
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
          <div className={styles.hubCardTitle}>{name}</div>
          <div className={styles.hubCardMeta}>{meta}</div>
        </div>
      </div>
      {menu}
    </div>
    {description && (
      <div className={styles.hubCardDescription}>{description}</div>
    )}
    {note && (
      <div className={styles.hubCardDescription} role="note">
        {note}
      </div>
    )}
    {extra}
    {chips && <div className={styles.hubCardChips}>{chips}</div>}
    <div className={styles.hubCardFooter}>
      <Group gap={6} className={styles.hubCardPills}>
        {pills}
      </Group>
      <div className={styles.hubCardAction}>{action}</div>
    </div>
  </div>
);
