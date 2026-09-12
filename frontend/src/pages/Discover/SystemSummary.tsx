import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";
import { Anchor, Text, Title } from "@mantine/core";
import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faArrowRight,
  faArrowsRotate,
  faClosedCaptioning,
  faGears,
  faMoon,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverSummary } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type {
  DiscoverActivityItem,
  DiscoverArrival,
  DiscoverAttentionItem,
  DiscoverWantedComponent,
} from "@/types/discover";
import styles from "./Discover.module.scss";

// The rail follows the approved Discover homepage concept: a current-work
// block, then recent arrivals with their History route, then Wanted demand
// with its plain label, then a scope and freshness footer. It is a seam in
// the page surface, not a card. The concept's sample strings are illustrative
// only, so every line here is rendered from the live summary: the shape and
// order are the concept's, the words are this Bazarr's own.
//
// The concept draws its own sidebar and drawer. The application already owns
// grouped navigation in the AppShell, so this rail links only to real routes
// and never renders a parallel nav: the generic local-pages list is gone, and
// what remains are scoped entries (queue, history, Wanted, recovery actions)
// that each carry a return target back to the exact Discover task.

// The sidebar becomes a wrapped row below this width, where a full panel would
// push discovery itself off the first screen.
const NARROW_VIEWPORT = "(max-width: 900px)";

// The reader's disclosure choice, remembered per browser like their chosen
// subtitle language. Absent means "follow the viewport".
const DISCLOSURE_KEY = "bazarr.discover.local-work-detail";

// The concept's review states, driven by live data. Degraded work still runs,
// so it reads as attention; a reading that never arrived is stale, the same
// way a retained one is, and the copy inside says which of the two it is.
type SystemKey = "busy" | "quiet" | "attention" | "new" | "stale";

function readDisclosure(): boolean | null {
  try {
    const stored = localStorage.getItem(DISCLOSURE_KEY);
    return stored === null ? null : stored === "open";
  } catch {
    return null;
  }
}

const numeric = { fontVariantNumeric: "tabular-nums" } as const;
const atomic = { whiteSpace: "nowrap" } as const;

// Amber is the primary action. In this panel only a recovery route for a
// real problem earns it (styles.panelRecovery); every navigational link is
// quiet (styles.panelLink).

function useNarrowViewport() {
  const [narrow, setNarrow] = useState(() => {
    try {
      return window.matchMedia(NARROW_VIEWPORT).matches;
    } catch {
      return false;
    }
  });
  useEffect(() => {
    let query: MediaQueryList;
    try {
      query = window.matchMedia(NARROW_VIEWPORT);
    } catch {
      return;
    }
    const update = (event: MediaQueryListEvent) => setNarrow(event.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return narrow;
}

function plural(count: number, one: string, many: string) {
  return `${count} ${count === 1 ? one : many}`;
}

function episodeNumber(item: {
  season: number | null;
  episode: number | null;
}) {
  if (item.season === null || item.episode === null) return null;
  return `S${String(item.season).padStart(2, "0")} E${String(item.episode).padStart(2, "0")}`;
}

// A relative age for the footer and the quiet block. Absolute stamps stay on
// the arrivals themselves, where the exact time matters.
function ago(timestamp: string | null): string | null {
  if (!timestamp) return null;
  const moment = Date.parse(timestamp);
  if (Number.isNaN(moment)) return null;
  const minutes = Math.floor(Math.max(0, Date.now() - moment) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes === 1) return "a minute ago";
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  if (hours === 1) return "an hour ago";
  if (hours < 24) return `${hours} hours ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "a day ago";
  if (days < 30) return `${days} days ago`;
  return new Date(moment).toLocaleString([], {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function scopeLine(item: DiscoverActivityItem) {
  const parts = [
    item.instance_name,
    item.title,
    episodeNumber(item),
    item.language,
  ].filter((part): part is string => Boolean(part));
  return parts.length ? parts.join(" · ") : null;
}

// Measured progress is drawn as a thin amber bar with its figure right
// aligned above it, the way the concept draws it. Unmeasured work gets
// words, never a bar: a bar with no source behind it would invent a
// measurement.
function TaskProgress({ item }: { item: DiscoverActivityItem }) {
  if (item.phase === "waiting_for_service" || !item.progress) return null;
  const { unit, value, total } = item.progress;
  const text = unit === "percent" ? `${value}%` : `${value} of ${total}`;
  return (
    <>
      <div className={styles.progressLine}>
        <Text
          size="xs"
          className={styles.progressFigure}
          style={{ ...atomic, ...numeric }}
        >
          {text}
        </Text>
      </div>
      <progress
        className={styles.progress}
        max={unit === "percent" ? 100 : total}
        value={value}
        aria-label={`Work progress, ${text}`}
      >
        {text}
      </progress>
    </>
  );
}

function ArrivalLine({ item }: { item: DiscoverArrival }) {
  const number = episodeNumber(item);
  let language = item.language;
  if (language) {
    const [code, ...variants] = language.split(":");
    try {
      language = [
        new Intl.DisplayNames(["en"], { type: "language" }).of(code),
        ...variants,
      ].join(" · ");
    } catch {
      /* Keep an unrecognized provider language as supplied. */
    }
  }
  const provenance = [language, item.provider, item.instance_name]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
  // A live age beside the provenance, the way the concept rows read. The
  // exact stamp stays on the element for assistive technology and tooltips.
  const age = ago(item.timestamp);
  const absolute =
    item.timestamp !== null && !Number.isNaN(Date.parse(item.timestamp))
      ? new Date(item.timestamp).toLocaleString([], {
          dateStyle: "short",
          timeStyle: "short",
        })
      : null;
  return (
    <li className={styles.arrival}>
      <span className={styles.arrivalArt} aria-hidden="true">
        {item.poster_url ? (
          <img
            src={item.poster_url}
            alt=""
            loading="lazy"
            onError={(event) => {
              event.currentTarget.style.display = "none";
            }}
          />
        ) : (
          <FontAwesomeIcon icon={faClosedCaptioning} />
        )}
      </span>
      <div className={styles.arrivalBody}>
        <Text
          size="sm"
          className={styles.arrivalTitle}
          style={{ overflowWrap: "anywhere" }}
        >
          <strong>{item.title ?? "Unnamed media"}</strong>
          {number !== null && (
            <>
              {" · "}
              <span style={atomic}>{number}</span>
            </>
          )}
        </Text>
        {item.episode_title !== null && (
          <Text
            size="xs"
            c="dimmed"
            className={styles.arrivalMeta}
            style={{ overflowWrap: "anywhere" }}
          >
            {item.episode_title}
          </Text>
        )}
        {(provenance || age !== null) && (
          <Text
            size="xs"
            c="dimmed"
            className={styles.arrivalMeta}
            style={{ overflowWrap: "anywhere" }}
          >
            {provenance}
            {provenance && age !== null ? " · " : null}
            {age !== null && item.timestamp !== null && (
              <time
                dateTime={item.timestamp}
                title={absolute ?? undefined}
                style={{ ...atomic, ...numeric }}
              >
                {age}
              </time>
            )}
          </Text>
        )}
      </div>
    </li>
  );
}

function AttentionLine({
  item,
  remember,
}: {
  item: DiscoverAttentionItem;
  remember: () => void;
}) {
  return (
    <li style={{ marginBottom: 12 }}>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        {item.summary}
      </Text>
      {item.detail !== null && (
        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          {item.detail}
        </Text>
      )}
      {item.freshness === "last_recorded_observation" && (
        <Text size="xs" c="dimmed">
          Last recorded observation. Its age is unknown.
        </Text>
      )}
      <Anchor
        className={styles.panelRecovery}
        component={Link}
        to={item.recovery.target}
        size="sm"
        onClick={remember}
      >
        {item.recovery.label}
      </Anchor>
    </li>
  );
}

function wantedTarget(wanted: DiscoverWantedComponent) {
  return (wanted.movie_requirements ?? 0) > (wanted.episode_requirements ?? 0)
    ? "/wanted/movies"
    : "/wanted/series";
}

function Group({
  label,
  icon,
  tone,
  children,
}: {
  label: string;
  icon: IconDefinition;
  tone?: "attention";
  children: React.ReactNode;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={styles.panelGroup}
      data-tone={tone}
    >
      <div className={styles.panelGroupHead}>
        <FontAwesomeIcon icon={icon} className={styles.panelIcon} />
        <Text component="span" className={styles.panelGroupLabel}>
          {label}
        </Text>
      </div>
      <div className={styles.panelGroupBody}>{children}</div>
    </div>
  );
}

// A count that could not be read is said, never drawn as a zero.
function Count({ label, count }: { label: string; count: number | null }) {
  return (
    <span className={styles.count}>
      <strong className={styles.countValue} data-unknown={count === null}>
        {count === null ? "Unknown" : count}
      </strong>{" "}
      <span className={styles.countLabel}>{label}</span>
    </span>
  );
}

// A panel beside the page, not a dashboard: it lists a few items and says how
// many more there are rather than growing without limit.
const PANEL_LIMIT = 3;

const bareList = { listStyle: "none", padding: 0, margin: 0 } as const;

export default function SystemSummary({
  showArrivals = true,
}: {
  showArrivals?: boolean;
}) {
  const summary = useDiscoverSummary();
  const { updateBrowsing } = useDiscover();
  const location = useLocation();
  const narrow = useNarrowViewport();
  // Component state resets on remount, so leaving for a local page and coming
  // back used to collapse the panel again. On a narrow viewport that shrinks
  // the document by roughly a thousand pixels, which is what made the saved
  // return offset unreachable. The reader's own choice is remembered the same
  // way their subtitle language is, so the page comes back the height it left.
  const [override, setOverride] = useState<boolean | null>(readDisclosure);
  // Desktop has no disclosure control, so its contents must always be open.
  // Keep the narrow-screen choice for the next time the viewport shrinks.
  const open = !narrow || (override ?? false);
  // Only an actual interaction is a choice. A toggle event also fires when the
  // element is opened by its own default, so persisting from there would store
  // a preference the reader never expressed.
  const rememberDisclosure = (next: boolean) => {
    try {
      localStorage.setItem(DISCLOSURE_KEY, next ? "open" : "closed");
    } catch {
      // A viewer with storage disabled keeps the choice for this visit only.
    }
  };

  // Record where to come back to, and nothing else. The Discover page already
  // tracks the retrieval control the reader was working in; naming a link here
  // as the continuation point would return focus to the link instead.
  const remember = () =>
    updateBrowsing({
      returnTarget: location.pathname + location.search + location.hash,
    });

  const data = summary.data;
  const unavailable = data === undefined;
  // A failed refresh keeps the last reading rather than dropping to zero, but
  // it has to say so: a retained value presented as current is a lie about how
  // fresh it is, and zero work and no answer are not the same fact.
  const stale = summary.isError && data !== undefined;
  const activity = data?.activity;
  const wanted = data?.wanted;

  const available = !unavailable && activity?.availability === "available";
  const runningCount = activity?.running_count ?? null;
  const queuedCount = activity?.queued_count ?? null;
  const scheduledCount = activity?.scheduled_count ?? null;
  const idle = available && runningCount === 0 && queuedCount === 0;
  const unknown =
    !unavailable &&
    !stale &&
    (!available ||
      data.state === "unknown" ||
      runningCount === null ||
      queuedCount === null);

  const systemKey: SystemKey = unavailable
    ? "stale"
    : stale
      ? "stale"
      : data.state === "busy"
        ? "busy"
        : data.state === "quiet"
          ? "quiet"
          : data.state === "degraded"
            ? "attention"
            : data.state === "new_installation"
              ? "new"
              : "stale";

  const featured = activity?.running[0];
  const moreRunning =
    runningCount !== null
      ? runningCount - (featured ? 1 : 0)
      : (activity?.running.length ?? 0) - (featured ? 1 : 0);
  const nextScheduled = activity?.scheduled[0] ?? null;
  const scheduleAge = ago(activity?.observed_at ?? null);
  const readingAge = ago(data?.generated_at ?? null);

  return (
    <aside
      className={styles.localSummary}
      aria-labelledby="discover-local-title"
      data-system={systemKey}
    >
      <div className={styles.panelHead}>
        <Title order={2} id="discover-local-title">
          Your Bazarr+
        </Title>
        <div className={styles.headActions}>
          <Anchor
            className={styles.panelLink}
            component="button"
            type="button"
            size="sm"
            ta="left"
            aria-label={
              summary.isError ? "Retry local status" : "Refresh local status"
            }
            title={
              summary.isError ? "Retry local status" : "Refresh local status"
            }
            onClick={() => void summary.refetch()}
          >
            <FontAwesomeIcon icon={faArrowsRotate} />
          </Anchor>
          <Anchor
            className={styles.panelLink}
            component={Link}
            to="/subtitle-hub"
            aria-label="Provider settings"
            onClick={remember}
          >
            <FontAwesomeIcon icon={faArrowRight} />
          </Anchor>
        </div>
      </div>

      {/* What is happening now. The concept's current-work block: one running
          task with its scope and measured progress, or the quiet schedule, or
          the setup call, or an honest unknown. */}
      <div
        id="bh-current"
        role="group"
        aria-label="Current work"
        className={styles.current}
      >
        {summary.isLoading ? (
          <Text size="sm">Reading local status.</Text>
        ) : unavailable ? (
          <>
            <Text size="sm">Local status is unavailable.</Text>
            <Text size="xs" className={styles.panelNote}>
              Counts are unknown, not zero. Nothing here has been checked.
            </Text>
          </>
        ) : (
          stale && (
            <Text size="xs" className={styles.panelNote}>
              Local status could not be refreshed. This is the last reading
              {readingAge ? `, taken ${readingAge}` : ""}.
            </Text>
          )
        )}
        {/* A new installation reads like any other quiet library: the status
            below reports what the summary actually measured, and the footer
            says no library is connected yet. There is no setup panel; the
            settings link in the head row carries provider and library
            setup. */}
        {!unavailable && !summary.isLoading && unknown && (
          <>
            <div className={styles.countsLine}>
              <Count label="running" count={runningCount} />
              <Count label="queued" count={queuedCount} />
              <Count label="scheduled" count={scheduledCount} />
            </div>
            <Text size="xs" className={styles.panelNote}>
              Part of this reading could not be taken. What is missing is
              unknown, not healthy.
            </Text>
          </>
        )}
        {!unavailable &&
          !summary.isLoading &&
          !unknown &&
          (idle ? (
            <>
              <div className={styles.workingTitle}>
                <FontAwesomeIcon icon={faMoon} className={styles.panelIcon} />
                <Text component="span">No jobs running.</Text>
              </div>
              {nextScheduled !== null && (
                <Text size="xs" c="dimmed" className={styles.taskScope}>
                  Next: {nextScheduled.name}
                  {nextScheduled.next_run_in
                    ? `, ${nextScheduled.next_run_in}`
                    : ""}
                  .
                </Text>
              )}
              {scheduleAge !== null && (
                <Text size="xs" className={styles.panelNote}>
                  Schedule checked {scheduleAge}.
                </Text>
              )}
            </>
          ) : (
            <>
              {featured ? (
                <>
                  <div className={styles.workingTitle}>
                    <FontAwesomeIcon
                      icon={faGears}
                      className={styles.panelIcon}
                    />
                    <Text component="span">Local work running</Text>
                  </div>
                  <Text
                    size="sm"
                    className={styles.taskTitle}
                    style={{ overflowWrap: "anywhere" }}
                  >
                    <strong>{featured.name}</strong>
                  </Text>
                  {scopeLine(featured) !== null && (
                    <Text
                      size="xs"
                      c="dimmed"
                      className={styles.taskScope}
                      style={{ overflowWrap: "anywhere" }}
                    >
                      {scopeLine(featured)}
                    </Text>
                  )}
                  {featured.phase === "waiting_for_service" && (
                    <Text
                      size="xs"
                      c="dimmed"
                      className={`${styles.taskScope} ${styles.taskMore}`}
                    >
                      Waiting for the translation service to start this job.
                    </Text>
                  )}
                  <TaskProgress item={featured} />
                  {moreRunning > 0 && (
                    <Text size="xs" c="dimmed" className={styles.taskMore}>
                      and {moreRunning} more running
                    </Text>
                  )}
                </>
              ) : (
                <div className={styles.countsLine}>
                  <Count label="running" count={runningCount} />
                  <Count label="queued" count={queuedCount} />
                  <Count label="scheduled" count={scheduledCount} />
                </div>
              )}
              {queuedCount !== null ? (
                queuedCount > 0 && (
                  <Anchor
                    className={`${styles.panelLink} ${styles.queueLink}`}
                    component={Link}
                    to="/system/tasks"
                    size="sm"
                    onClick={remember}
                  >
                    {plural(queuedCount, "job queued", "jobs queued")}{" "}
                    <FontAwesomeIcon icon={faArrowRight} size="xs" />
                  </Anchor>
                )
              ) : (
                <Text size="xs" className={styles.panelNote}>
                  Queued work could not be read.
                </Text>
              )}
            </>
          ))}
        {!unavailable &&
          activity &&
          activity.unknown_sources.includes("schedules") && (
            <Text size="xs" className={styles.panelNote}>
              Scheduled work could not be read. Its state is unknown, not none.
            </Text>
          )}
      </div>

      {data !== undefined && (
        <details role="group" aria-label="Local work detail" open={open}>
          <Text
            component="summary"
            size="sm"
            className={styles.panelSummary}
            onClick={(event) => {
              event.preventDefault();
              if (!narrow) return;
              setOverride(!open);
              rememberDisclosure(!open);
            }}
          >
            {showArrivals
              ? "Recent arrivals and Wanted"
              : "Wanted and activity"}
          </Text>

          <div id="bh-system-secondary">
            {data.attention.availability !== "available" && (
              <Group
                label="Needs attention"
                icon={faTriangleExclamation}
                tone="attention"
              >
                <Text size="sm">
                  Scoped problems could not be read. This is not a report that
                  nothing is wrong.
                </Text>
              </Group>
            )}

            {/* One branch, one list, one overflow line. Two branches that each
                rendered the list were free to disagree about it, and they did:
                the incomplete-sources copy silently dropped the overflow
                count. */}
            {data.attention.availability === "available" &&
              (data.attention.unknown_sources.length > 0 ||
                data.attention.items.length > 0) && (
                <Group
                  label="Needs attention"
                  icon={faTriangleExclamation}
                  tone="attention"
                >
                  {data.attention.unknown_sources.length > 0 && (
                    <Text size="sm">
                      {data.attention.items.length
                        ? "Some sources could not be checked, so this list may be incomplete."
                        : "Some sources could not be checked. Nothing is reported, which is not the same as nothing being wrong."}
                    </Text>
                  )}
                  <ul style={bareList}>
                    {data.attention.items.slice(0, PANEL_LIMIT).map((item) => (
                      <AttentionLine
                        key={item.id}
                        item={item}
                        remember={remember}
                      />
                    ))}
                  </ul>
                  {data.attention.items.length > PANEL_LIMIT && (
                    <Text size="xs" c="dimmed">
                      and {data.attention.items.length - PANEL_LIMIT} more to
                      review
                    </Text>
                  )}
                </Group>
              )}

            {showArrivals && (
              <div
                role="group"
                aria-label="Subtitles arrived"
                className={`${styles.panelGroup} ${styles.panelStack} ${styles.recent}`}
              >
                <div className={styles.recentHead}>
                  <Text component="span" className={styles.panelGroupLabel}>
                    Subtitles arrived
                  </Text>
                  <Anchor
                    className={styles.panelLink}
                    component={Link}
                    to="/history/series"
                    size="sm"
                    onClick={remember}
                  >
                    History <FontAwesomeIcon icon={faArrowRight} size="xs" />
                  </Anchor>
                </div>
                <div className={styles.panelGroupBody}>
                  {data.arrivals.length ? (
                    <ul style={bareList} className={styles.arrivalList}>
                      {data.arrivals.slice(0, PANEL_LIMIT).map((item) => (
                        <ArrivalLine key={item.event_id} item={item} />
                      ))}
                    </ul>
                  ) : (
                    <Text size="sm">
                      {data.arrivals_status.availability === "available"
                        ? "No subtitles have arrived recently."
                        : "Recent subtitle arrivals could not be read."}
                    </Text>
                  )}
                </div>
              </div>
            )}

            {wanted && (
              <div
                role="group"
                aria-label="Wanted"
                className={`${styles.panelGroup} ${styles.panelStack}`}
              >
                {wanted.requirements === null ? (
                  <Text size="sm">
                    Unknown. The outstanding requirement count could not be
                    read.
                  </Text>
                ) : (
                  <>
                    <Anchor
                      className={styles.wantedRow}
                      component={Link}
                      to={wantedTarget(wanted)}
                      onClick={remember}
                    >
                      <span>
                        <strong>Wanted</strong>
                        <small>Subtitles still needed</small>
                      </span>
                      <strong className={styles.bigNumber}>
                        {wanted.complete
                          ? wanted.requirements
                          : `At least ${wanted.requirements}`}
                      </strong>
                    </Anchor>
                    {wanted.media_count !== null && (
                      <Text size="xs" c="dimmed">
                        across{" "}
                        {plural(
                          wanted.media_count,
                          "library item",
                          "library items",
                        )}
                      </Text>
                    )}
                    {wanted.unknown_media_count ? (
                      <Text size="xs" c="dimmed">
                        {plural(
                          wanted.unknown_media_count,
                          "item has",
                          "items have",
                        )}{" "}
                        no computed requirement list yet.
                      </Text>
                    ) : null}
                    {wanted.qualifications.includes(
                      "malformed_requirements",
                    ) && (
                      <Text size="xs" c="dimmed">
                        Some stored requirement lists could not be read.
                      </Text>
                    )}
                  </>
                )}
              </div>
            )}

            <p className={styles.localFoot}>
              {data.state === "new_installation"
                ? "No library connected yet"
                : "All configured libraries"}
              {readingAge ? ` · Updated ${readingAge}` : ""}
            </p>
          </div>
        </details>
      )}
    </aside>
  );
}
