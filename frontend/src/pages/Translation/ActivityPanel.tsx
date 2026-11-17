import { FunctionComponent, useId, useState } from "react";
import { Button, SegmentedControl, Skeleton } from "@mantine/core";
import { faChevronRight, faLanguage } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useTranslationHistory,
  useTranslationQueue,
} from "@/apis/hooks/translation";
import { EmptyState } from "@/components/hub/EmptyState";
import { StatusBadge } from "@/components/hub/StatusBadge";
import { formatAbsoluteTime, formatRelativeTime } from "@/components/hub/utils";
import type {
  TranslationHistoryJob,
  TranslationState,
} from "@/types/translation";
import { backendLabel, engineName, JOB_STATUS, money, tokens } from "./model";
import styles from "./Translation.module.scss";

const PAGE = 50;
/** The most the history route returns in one answer. */
const MOST = 200;

const FILTERS = [
  { value: "all", label: "All" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "awaiting_retry", label: "Waiting to retry" },
];

let languageNames: Intl.DisplayNames | null = null;

/** "en" as "English". A code the browser does not know is shown as it is. */
function language(code: string | null): string {
  if (!code) {
    return "Unknown";
  }
  try {
    languageNames ??= new Intl.DisplayNames(["en"], { type: "language" });
    return languageNames.of(code) ?? code;
  } catch {
    return code;
  }
}

const JobRow: FunctionComponent<{
  job: TranslationHistoryJob;
  state: TranslationState;
}> = ({ job, state }) => {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const engine = state.instances.find(
    (item) => item.id === job.engine_instance_id,
  );
  // A back end that still exists goes by the name the Engine tab gives it.
  const typeLabel = (type: string, connectionId: number | null = null) => {
    const current = engine?.connections.find(
      (item) => item.id === connectionId,
    );
    return current && engine
      ? backendLabel(engine, current)
      : (engine?.schema?.backend_types[type]?.label ?? type);
  };
  const status = JOB_STATUS[job.status] ?? {
    tone: "neutral" as const,
    label: job.status,
  };
  const used = job.connections.filter((item) => item.batches > 0);
  const main = used[used.length - 1] ?? job.connections[0];
  const when = job.finished_at ?? job.started_at;
  const pair = `${language(job.source_language)} to ${language(job.target_language)}`;

  return (
    <li className={styles.job}>
      <button
        type="button"
        className={styles.jobButton}
        aria-expanded={open}
        // The detail exists only while the row is open: closed, there is nothing to point at.
        aria-controls={open ? detailId : undefined}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.jobTitle}>
          {job.media_title || "Untitled"}
          <span className={styles.jobSub}>
            {pair}
            {when
              ? `, ${job.finished_at ? "" : "started "}${formatRelativeTime(when)}`
              : ""}
            <span className={styles.narrow}>{`, ${money(job.cost)}`}</span>
          </span>
        </span>
        <span className={`${styles.jobCell} ${styles.wide}`}>
          {main ? (
            <>
              {typeLabel(main.backend_type, main.connection_id)}
              <span className={styles.jobSub}>
                {main.models.join(", ") || "No model recorded"}
              </span>
            </>
          ) : (
            (engine && engineName(engine)) || "No back end used"
          )}
        </span>
        <span className={`${styles.jobNumber} ${styles.wide}`}>
          {tokens(job.input_tokens + job.output_tokens)}
        </span>
        <span className={`${styles.jobNumber} ${styles.wide}`}>
          {money(job.cost)}
        </span>
        <span className={styles.jobStatus}>
          <StatusBadge tone={status.tone} label={status.label} />
        </span>
        <FontAwesomeIcon
          icon={faChevronRight}
          className={`${styles.jobChevron} ${styles.wide}`}
        />
      </button>
      {open && (
        <div className={styles.jobDetail} id={detailId}>
          <dl>
            <dt>Languages</dt>
            <dd>{pair}</dd>
            <dt>Engine</dt>
            <dd>
              {engine ? engineName(engine) : (job.plugin_id ?? "Unknown")}
              {job.plugin_version ? ` v${job.plugin_version}` : ""}
            </dd>
            <dt>Started</dt>
            <dd>{formatAbsoluteTime(job.started_at) || "Not recorded"}</dd>
            <dt>Cost</dt>
            <dd>
              {money(job.cost)} for {job.input_tokens.toLocaleString("en-US")}{" "}
              tokens in and {job.output_tokens.toLocaleString("en-US")} out
            </dd>
            {job.connections.map((item, index) => (
              <div key={index} style={{ display: "contents" }}>
                <dt>{typeLabel(item.backend_type, item.connection_id)}</dt>
                <dd>
                  {item.models.join(", ") || "No model recorded"}
                  {`, ${item.batches} ${item.batches === 1 ? "batch" : "batches"}, ${money(item.cost)}`}
                  {item.failed_over
                    ? `. Handed over to the next back end${
                        item.last_error_class
                          ? ` (${item.last_error_class})`
                          : ""
                      }.`
                    : ""}
                </dd>
              </div>
            ))}
            {job.declined_reason && (
              <>
                <dt>Declined</dt>
                <dd>{job.declined_reason}</dd>
              </>
            )}
            {(job.error_class || job.error_detail) && (
              <>
                <dt>Error</dt>
                <dd className={styles.error}>
                  {job.error_detail || job.error_class}
                  {job.error_detail && job.error_class
                    ? ` (${job.error_class})`
                    : ""}
                </dd>
              </>
            )}
          </dl>
        </div>
      )}
    </li>
  );
};

/**
 * A list that could not be loaded, or could not be updated. It says which, and offers the way to ask
 * again: a section that is simply missing would read as "nothing is running".
 */
const LoadProblem: FunctionComponent<{
  /** The list, as the sentence starts: "The running jobs". */
  what: string;
  /** Rows from an earlier load are still shown. */
  stale: boolean;
  busy: boolean;
  onRetry: () => void;
}> = ({ what, stale, busy, onRetry }) => (
  <div className={styles.loadProblem} role="alert">
    <span>
      {stale
        ? `${what} could not be updated. What is listed may be out of date.`
        : `${what} could not be loaded.`}
    </span>
    <Button
      size="compact-xs"
      variant="default"
      loading={busy}
      onClick={onRetry}
    >
      Try again
    </Button>
  </div>
);

const JobList: FunctionComponent<{
  jobs: TranslationHistoryJob[];
  state: TranslationState;
  label: string;
  /** The rows come from an earlier load that could not be updated. */
  stale?: boolean;
}> = ({ jobs, state, label, stale = false }) => (
  <>
    <div className={styles.jobsHead} aria-hidden="true">
      <span>Subtitle</span>
      <span>Back end</span>
      <span className={styles.right}>Tokens</span>
      <span className={styles.right}>Cost</span>
      <span className={styles.right}>Status</span>
      <span />
    </div>
    <ul className={styles.jobs} aria-label={label} data-stale={stale}>
      {jobs.map((job) => (
        <JobRow key={job.id} job={job} state={state} />
      ))}
    </ul>
  </>
);

/** What is being translated now, and what was: each job with its back end, tokens, cost and outcome. */
const ActivityPanel: FunctionComponent<{
  state: TranslationState;
  /** The tab is the one on screen. A hidden tab keeps what it has and stops asking for more. */
  active?: boolean;
  /** Whether the server lists the jobs that have not started yet. */
  canQueue?: boolean;
}> = ({ state, active = true, canQueue = false }) => {
  const [filter, setFilter] = useState("all");
  const [length, setLength] = useState(PAGE);
  const running = useTranslationHistory("running", PAGE, active ? 5000 : false);
  const history = useTranslationHistory(
    filter === "all" ? null : filter,
    length,
    active ? 30_000 : false,
  );
  const queue = useTranslationQueue(canQueue, active);
  const waiting = queue.data?.data ?? [];
  const now = running.data?.data ?? [];
  // A running job is shown once, in the section above.
  const past = (history.data?.data ?? []).filter(
    (job) => job.status !== "running",
  );
  const total = history.data?.total ?? 0;

  return (
    <div>
      {(now.length > 0 || running.isError) && (
        <section className={styles.section}>
          <div className={styles.sectionHead}>
            <h2 className={styles.sectionTitle}>Translating now</h2>
          </div>
          {running.isError && (
            <LoadProblem
              what="The running jobs"
              stale={now.length > 0}
              busy={running.isFetching}
              onRetry={() => void running.refetch()}
            />
          )}
          {now.length > 0 && (
            <JobList
              jobs={now}
              state={state}
              label="Translating now"
              stale={running.isError}
            />
          )}
        </section>
      )}

      {(waiting.length > 0 || queue.isError) && (
        <section className={styles.section}>
          <div className={styles.sectionHead}>
            <h2 className={styles.sectionTitle}>Waiting</h2>
          </div>
          {queue.isError && (
            <LoadProblem
              what="The waiting jobs"
              stale={waiting.length > 0}
              busy={queue.isFetching}
              onRetry={() => void queue.refetch()}
            />
          )}
          <ul
            className={styles.jobs}
            aria-label="Waiting"
            data-stale={queue.isError}
            hidden={waiting.length === 0}
          >
            {waiting.map((item, index) => (
              <li key={item.job_id} className={styles.waiting}>
                <span className={styles.position} aria-hidden="true">
                  {index + 1}
                </span>
                <span className={styles.jobTitle}>
                  {item.media_title || "Untitled"}
                  <span className={styles.jobSub}>
                    {`${language(item.source_language)} to ${language(item.target_language)}`}
                    {item.queued_at
                      ? `, queued ${formatRelativeTime(item.queued_at)}`
                      : ""}
                  </span>
                </span>
                <StatusBadge
                  tone={
                    item.status === "awaiting_retry" ? "warning" : "neutral"
                  }
                  label={
                    item.status === "awaiting_retry"
                      ? "Waiting to retry"
                      : "Queued"
                  }
                />
              </li>
            ))}
          </ul>
          {(queue.data?.total ?? 0) > waiting.length && (
            <p className={styles.sectionHint}>
              {`And ${(queue.data?.total ?? 0) - waiting.length} more.`}
            </p>
          )}
        </section>
      )}

      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>History</h2>
          <SegmentedControl
            size="xs"
            aria-label="Show"
            data={FILTERS}
            value={filter}
            onChange={(next) => {
              setFilter(next);
              setLength(PAGE);
            }}
          />
        </div>
        {history.isLoading ? (
          <div aria-busy="true" style={{ display: "grid", gap: 8 }}>
            {[0, 1, 2, 3].map((item) => (
              <Skeleton key={item} height={44} radius="sm" />
            ))}
          </div>
        ) : (
          <>
            {/* A failed load is said first, whatever is under it: rows, or an answer that there are none. */}
            {history.isError && (
              <LoadProblem
                what="The history"
                stale={history.data !== undefined}
                busy={history.isFetching}
                onRetry={() => void history.refetch()}
              />
            )}
            {history.data !== undefined &&
              past.length === 0 &&
              (filter === "all" ? (
                <EmptyState
                  icon={faLanguage}
                  title="Nothing has been translated yet"
                  body="Translated subtitles show up here, each with the back end that did it and what it cost."
                />
              ) : (
                <div className={styles.quiet}>No jobs match this filter.</div>
              ))}
            {past.length > 0 && (
              <>
                <JobList
                  jobs={past}
                  state={state}
                  label="History"
                  stale={history.isError}
                />
                {total > length && length < MOST && (
                  <Button
                    mt="md"
                    variant="default"
                    loading={history.isFetching}
                    onClick={() =>
                      setLength((value) => Math.min(value + PAGE, MOST))
                    }
                  >
                    Show more
                  </Button>
                )}
                {total > MOST && length >= MOST && (
                  <p className={styles.sectionHint}>
                    Showing the newest {MOST} of {total.toLocaleString("en-US")}{" "}
                    jobs.
                  </p>
                )}
              </>
            )}
          </>
        )}
      </section>
    </div>
  );
};

export default ActivityPanel;
