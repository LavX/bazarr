import { FunctionComponent, useState } from "react";
import { Badge, Button, Collapse, CopyButton, Tooltip } from "@mantine/core";
import {
  faChevronRight,
  faCopy,
  faStopwatch,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import type { ProviderHubJob } from "@/apis/raw/providerHub";
import {
  formatAbsoluteTime,
  formatDuration,
  formatRelativeTime,
  getActionLabel,
  getJobStateMeta,
} from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface ActivityRowProps {
  job: ProviderHubJob;
}

function jobTargetSummary(job: ProviderHubJob): string | null {
  const name = job.target_name ?? job.target_id ?? null;
  if (!name) return null;
  switch (job.target_kind) {
    case "provider":
      return `Plugin: ${name}`;
    case "source":
      return `Source: ${name}`;
    default:
      return name;
  }
}

function versionBadgeText(job: ProviderHubJob): string | null {
  const from = job.from_version;
  const to = job.to_version;
  if (from && to && from !== to) return `${from} → ${to}`;
  if (to) return `v${to}`;
  if (from) return `v${from}`;
  return null;
}

export const ActivityRow: FunctionComponent<ActivityRowProps> = ({ job }) => {
  const [open, setOpen] = useState(false);
  const meta = getJobStateMeta(job.state);
  const isFailed = job.state === "failed";
  const isRunning = job.state === "running" || job.state === "pending";
  const time = formatRelativeTime(job.updated_at ?? job.created_at);
  const absoluteTime = formatAbsoluteTime(job.updated_at ?? job.created_at);
  const target = jobTargetSummary(job);
  const versionText = versionBadgeText(job);
  const duration = formatDuration(job.duration_ms);

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
            className={isRunning ? styles.spin : undefined}
          />
        </div>
        <div className={styles.activityRowBody}>
          <div className={styles.activityRowHeadline}>
            <span className={styles.activityRowAction}>
              {getActionLabel(job.action)}
            </span>
            {target && (
              <span className={styles.activityRowTarget}>{target}</span>
            )}
            {versionText && (
              <Badge
                size="xs"
                radius="sm"
                variant="light"
                color={isFailed ? "red" : "blue"}
              >
                {versionText}
              </Badge>
            )}
            {duration && !isRunning && (
              <span className={styles.activityRowDuration}>
                <FontAwesomeIcon icon={faStopwatch} /> {duration}
              </span>
            )}
          </div>
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
          {target && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Target</span>
              <span className={styles.activityDetailValue}>{target}</span>
            </div>
          )}
          {(job.from_version || job.to_version) && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Version</span>
              <span className={styles.activityDetailValue}>
                {job.from_version && job.to_version
                  ? `${job.from_version} → ${job.to_version}`
                  : (job.to_version ?? job.from_version)}
              </span>
            </div>
          )}
          {job.started_at && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Started</span>
              <span className={styles.activityDetailValue}>
                {formatAbsoluteTime(job.started_at)}
              </span>
            </div>
          )}
          {job.completed_at && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Finished</span>
              <span className={styles.activityDetailValue}>
                {formatAbsoluteTime(job.completed_at)}
              </span>
            </div>
          )}
          {duration && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Duration</span>
              <span className={styles.activityDetailValue}>{duration}</span>
            </div>
          )}
          {job.message && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Message</span>
              <span className={styles.activityDetailValue}>{job.message}</span>
            </div>
          )}
          {job.error && (
            <div className={styles.activityDetailRow}>
              <span className={styles.activityDetailLabel}>Error</span>
              <pre className={styles.activityDetailError}>{job.error}</pre>
            </div>
          )}
          {(job.message || job.error) && (
            <CopyButton value={job.error ?? job.message ?? ""}>
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
                  {copied ? "Copied" : "Copy details"}
                </Button>
              )}
            </CopyButton>
          )}
        </div>
      </Collapse>
    </div>
  );
};
