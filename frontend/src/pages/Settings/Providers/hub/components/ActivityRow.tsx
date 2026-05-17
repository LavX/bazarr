import { FunctionComponent, useState } from "react";
import { Button, Collapse, CopyButton, Tooltip } from "@mantine/core";
import { faChevronRight, faCopy } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import type { ProviderHubJob } from "@/apis/raw/providerHub";
import {
  formatAbsoluteTime,
  formatRelativeTime,
  getJobStateMeta,
} from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface ActivityRowProps {
  job: ProviderHubJob;
}

function humanizeAction(action?: string): string {
  if (!action) return "Unknown action";
  return action.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export const ActivityRow: FunctionComponent<ActivityRowProps> = ({ job }) => {
  const [open, setOpen] = useState(false);
  const meta = getJobStateMeta(job.state);
  const isFailed = job.state === "failed";
  const time = formatRelativeTime(job.updated_at ?? job.created_at);
  const absoluteTime = formatAbsoluteTime(job.updated_at ?? job.created_at);

  return (
    <div>
      <div
        className={clsx(
          styles.activityRow,
          isFailed && styles.activityRowFailed,
          open && styles.activityRowOpen,
        )}
        onClick={() => setOpen((o) => !o)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((o) => !o);
          }
        }}
        aria-expanded={open}
      >
        <div
          className={styles.activityRowIcon}
          style={{
            color: `var(--bz-stat-${meta.tone === "success" ? "completed" : meta.tone === "danger" ? "failed" : meta.tone === "warning" ? "processing" : "queued"})`,
          }}
        >
          <FontAwesomeIcon
            icon={meta.icon}
            className={job.state === "running" ? styles.spin : undefined}
          />
        </div>
        <div className={styles.activityRowBody}>
          <span className={styles.activityRowAction}>
            {humanizeAction(job.action)}
          </span>
          {job.message && (
            <span className={styles.activityRowMessage}>{job.message}</span>
          )}
        </div>
        <Tooltip label={absoluteTime || "Unknown time"}>
          <span className={styles.activityRowTime}>{time || "—"}</span>
        </Tooltip>
        <FontAwesomeIcon
          icon={faChevronRight}
          className={styles.activityRowChevron}
        />
      </div>
      <Collapse expanded={open}>
        <div className={styles.activityDetail}>
          <div className={styles.activityDetailRow}>
            <span className={styles.activityDetailLabel}>Status</span>
            <span className={styles.activityDetailValue}>{meta.label}</span>
          </div>
          {job.created_at && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Started</span>
              <span className={styles.activityDetailValue}>
                {formatAbsoluteTime(job.created_at)}
              </span>
            </div>
          )}
          {job.updated_at && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Updated</span>
              <span className={styles.activityDetailValue}>
                {formatAbsoluteTime(job.updated_at)}
              </span>
            </div>
          )}
          {job.message && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Message</span>
              <span className={styles.activityDetailValue}>{job.message}</span>
            </div>
          )}
          {job.message && (
            <CopyButton value={job.message}>
              {({ copy, copied }) => (
                <Button
                  size="xs"
                  variant="subtle"
                  onClick={(e) => {
                    e.stopPropagation();
                    copy();
                  }}
                  leftSection={<FontAwesomeIcon icon={faCopy} />}
                  style={{ alignSelf: "flex-start" }}
                >
                  {copied ? "Copied" : "Copy message"}
                </Button>
              )}
            </CopyButton>
          )}
        </div>
      </Collapse>
    </div>
  );
};
