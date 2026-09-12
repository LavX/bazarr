import { Tooltip } from "@mantine/core";
import {
  faListCheck,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemJobs } from "@/apis/hooks";
import styles from "./AppShell.module.scss";

export default function JobsButton({
  onClick,
  expanded = false,
}: {
  onClick: () => void;
  expanded?: boolean;
}) {
  const { data, isPending, isError } = useSystemJobs();
  const jobs = Array.isArray(data) ? data : [];
  const running = jobs.filter((job) => job.status === "running");
  const queued = jobs.filter((job) => job.status === "pending");
  const active = [...running, ...queued];
  const unavailable = isError || (!isPending && !Array.isArray(data));
  const summary = unavailable
    ? "Job status unavailable"
    : isPending
      ? "Loading job status"
      : active.length
        ? [
            running.length ? `${running.length} running` : "",
            queued.length ? `${queued.length} queued` : "",
          ]
            .filter(Boolean)
            .join(" · ")
        : "No jobs running";
  const label = unavailable
    ? "Unavailable"
    : running.length
      ? `${running.length} running`
      : queued.length
        ? `${queued.length} queued`
        : "";
  return (
    <Tooltip
      label={
        <>
          <div>{summary}</div>
          {!unavailable &&
            active
              .slice(0, 3)
              .map((job) => <div key={job.job_id}>{job.job_name}</div>)}
        </>
      }
      position={expanded ? "top" : "right"}
      openDelay={250}
    >
      <button
        type="button"
        onClick={onClick}
        aria-label="Jobs Manager"
        aria-description={summary}
        className={`${expanded ? styles.expandedControls : styles.railLink} ${styles.jobsButton}`}
        data-active={!unavailable && running.length > 0}
      >
        {active.length > 0 && !unavailable ? (
          <span className={styles.jobActivity} aria-hidden="true">
            {active.slice(0, 3).map((job) => {
              const known =
                job.status === "running" &&
                job.is_progress &&
                Number.isFinite(job.progress_max) &&
                job.progress_max > 0 &&
                Number.isFinite(job.progress_value);
              const progress = known
                ? Math.max(
                    0,
                    Math.min(
                      100,
                      (job.progress_value / job.progress_max) * 100,
                    ),
                  )
                : undefined;
              return (
                <span
                  key={job.job_id}
                  className={styles.jobTrack}
                  data-running={job.status === "running"}
                  data-indeterminate={job.status === "running" && !known}
                >
                  <span
                    style={
                      progress === undefined
                        ? undefined
                        : { width: `${progress}%` }
                    }
                  />
                </span>
              );
            })}
          </span>
        ) : (
          <span className={styles.railIcon}>
            <FontAwesomeIcon
              icon={unavailable ? faTriangleExclamation : faListCheck}
            />
          </span>
        )}
        <span className={styles.jobCaption}>
          <span>{expanded ? "Jobs Manager" : "Jobs"}</span>
          {(expanded || label) && (
            <span className={styles.jobStatus}>
              {expanded ? summary : label}
            </span>
          )}
        </span>
      </button>
    </Tooltip>
  );
}
