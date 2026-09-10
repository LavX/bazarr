import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";
import { Anchor, Text, Title } from "@mantine/core";
import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faCircleArrowDown,
  faCompass,
  faGears,
  faListCheck,
  faSliders,
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

// The sidebar becomes a wrapped row below this width, where a full panel would
// push discovery itself off the first screen.
const NARROW_VIEWPORT = "(max-width: 1100px)";

// The reader's disclosure choice, remembered per browser like their chosen
// subtitle language. Absent means "follow the viewport".
const DISCLOSURE_KEY = "bazarr.discover.local-work-detail";

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
  return `S${String(item.season).padStart(2, "0")}E${String(item.episode).padStart(2, "0")}`;
}

function when(timestamp: string | null) {
  if (!timestamp) return null;
  const moment = new Date(timestamp);
  if (Number.isNaN(moment.getTime())) return null;
  // One unbreakable token on its own line. A date that wraps mid-value reads as
  // two different times, which is worse than showing nothing.
  return (
    <time dateTime={timestamp} style={{ ...atomic, ...numeric }}>
      {moment.toLocaleString([], { dateStyle: "short", timeStyle: "short" })}
    </time>
  );
}

function measured(item: DiscoverActivityItem) {
  if (item.phase === "waiting_for_service" || !item.progress) return null;
  const { unit, value, total } = item.progress;
  return (
    <Text component="span" size="xs" style={{ ...atomic, ...numeric }}>
      {unit === "percent" ? `${value}%` : `${value} of ${total}`}
    </Text>
  );
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

function ActivityLine({ item }: { item: DiscoverActivityItem }) {
  const scope = scopeLine(item);
  return (
    <li style={{ marginBottom: 10 }}>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        <strong>{item.name}</strong>
      </Text>
      {scope !== null && (
        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          {scope}
        </Text>
      )}
      {item.phase === "waiting_for_service" && (
        <Text size="xs" c="dimmed">
          Waiting for the translation service to start this job.
        </Text>
      )}
      {measured(item)}
    </li>
  );
}

function ArrivalLine({ item }: { item: DiscoverArrival }) {
  const number = episodeNumber(item);
  const provenance = [item.language, item.provider, item.instance_name]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
  return (
    <li style={{ marginBottom: 10 }}>
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        <strong>{item.title ?? "Unnamed media"}</strong>
        {number !== null && (
          <>
            {" · "}
            <span style={atomic}>{number}</span>
          </>
        )}
      </Text>
      {item.episode_title !== null && (
        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          {item.episode_title}
        </Text>
      )}
      {provenance && (
        <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>
          {provenance}
        </Text>
      )}
      {item.timestamp !== null && (
        <Text size="xs" c="dimmed">
          {when(item.timestamp)}
        </Text>
      )}
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
function Stat({ label, count }: { label: string; count: number | null }) {
  return (
    <span className={styles.stat}>
      <strong className={styles.statValue} data-unknown={count === null}>
        {count === null ? "Unknown" : count}
      </strong>
      <span className={styles.statLabel}>{label}</span>
    </span>
  );
}

// A panel beside the page, not a dashboard: it lists a few items and says how
// many more there are rather than growing without limit.
const PANEL_LIMIT = 3;

const bareList = { listStyle: "none", padding: 0, margin: 0 } as const;

export default function SystemSummary() {
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
  const open = override ?? !narrow;
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
  // The destinations the attention items already offer, so the generic rail
  // links below do not repeat one that is already named for a real problem.
  // Only the rendered slice counts: an item past the panel limit offers the
  // reader nothing, so suppressing the generic link on its behalf would leave
  // the rail with neither.
  const attentionTargets = new Set(
    (data?.attention.items ?? [])
      .slice(0, PANEL_LIMIT)
      .map((item) => item.recovery.target),
  );
  // A failed refresh keeps the last reading rather than dropping to zero, but
  // it has to say so: a retained value presented as current is a lie about how
  // fresh it is, and zero work and no answer are not the same fact.
  const stale = summary.isError && data !== undefined;
  const activity = data?.activity;
  const wanted = data?.wanted;

  const available = !unavailable && activity?.availability === "available";
  const idle = available && !activity.running_count && !activity.queued_count;

  return (
    <aside
      className={styles.localSummary}
      aria-labelledby="discover-local-title"
    >
      <div className={styles.panelHead}>
        <Title order={2} id="discover-local-title">
          Your Bazarr+
        </Title>
        <Anchor
          className={styles.panelLink}
          component="button"
          type="button"
          size="sm"
          ta="left"
          onClick={() => void summary.refetch()}
        >
          {summary.isError ? "Retry local status" : "Refresh local status"}
        </Anchor>
      </div>

      {/* What is happening now, as numbers. Running, queued and scheduled
          are three different states and stay three figures; a figure that
          could not be read says so. */}
      <div role="group" aria-label="Current work" className={styles.nowTile}>
        <FontAwesomeIcon icon={faGears} className={styles.panelIcon} />
        <div className={styles.nowBody}>
          {summary.isLoading ? (
            <Text size="sm">Reading local status.</Text>
          ) : unavailable ? (
            <Text size="sm">Local status is unavailable.</Text>
          ) : !available ? (
            <Text size="sm">Local work status is unknown right now.</Text>
          ) : (
            <div className={styles.statRow}>
              <Stat label="running" count={activity.running_count} />
              <Stat label="queued" count={activity.queued_count} />
              <Stat label="scheduled" count={activity.scheduled_count} />
            </div>
          )}
          {idle && (
            <Text size="xs" className={styles.panelNote}>
              No local work running.
            </Text>
          )}
          {unavailable && !summary.isLoading && (
            <Text size="xs" className={styles.panelNote}>
              Counts are unknown, not zero. Nothing here has been checked.
            </Text>
          )}
          {stale && (
            <Text size="xs" className={styles.panelNote}>
              Local status could not be refreshed. This is the last reading,
              taken {when(data.generated_at)}.
            </Text>
          )}
          {!unavailable && !stale && data.state === "new_installation" && (
            <Text size="xs" className={styles.panelNote}>
              Library automation is optional. Discover works without it.
            </Text>
          )}
          {!unavailable && !stale && data.state === "unknown" && (
            <Text size="xs" className={styles.panelNote}>
              Part of this reading could not be taken. What is missing is
              unknown, not healthy.
            </Text>
          )}
        </div>
      </div>

      <details
        role="group"
        aria-label="Local work detail"
        open={open}
        onToggle={(event) => setOverride(event.currentTarget.open)}
      >
        <Text
          component="summary"
          size="sm"
          className={styles.panelSummary}
          onClick={() => rememberDisclosure(!open)}
        >
          Local work detail
        </Text>

        {activity && activity.unknown_sources.includes("schedules") && (
          <Group label="Scheduled work" icon={faGears}>
            <Text size="sm">
              Scheduled work could not be read, so the scheduled figure is
              unknown rather than none.
            </Text>
          </Group>
        )}

        {activity && activity.running.length > 0 && (
          <Group label="Running now" icon={faGears}>
            <ul style={bareList}>
              {activity.running.slice(0, PANEL_LIMIT).map((item) => (
                <ActivityLine key={item.activity_id} item={item} />
              ))}
            </ul>
            {(activity.running_count ?? 0) > PANEL_LIMIT && (
              <Text size="xs" c="dimmed">
                and {(activity.running_count ?? 0) - PANEL_LIMIT} more running
              </Text>
            )}
          </Group>
        )}

        {wanted && (
          <Group label="Wanted subtitles" icon={faListCheck}>
            {wanted.requirements === null ? (
              <Text size="sm">
                Unknown. The outstanding requirement count could not be read.
              </Text>
            ) : (
              <>
                <Text size="sm" className={styles.wantedLine}>
                  {wanted.complete ? "" : "At least "}
                  <strong className={styles.bigNumber}>
                    {wanted.requirements}
                  </strong>{" "}
                  subtitle languages still wanted
                </Text>
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
                {wanted.qualifications.includes("malformed_requirements") && (
                  <Text size="xs" c="dimmed">
                    Some stored requirement lists could not be read.
                  </Text>
                )}
              </>
            )}
            <Anchor
              className={styles.panelLink}
              component={Link}
              to={wantedTarget(wanted)}
              size="sm"
              onClick={remember}
            >
              Open Wanted
            </Anchor>
          </Group>
        )}

        {data && (
          <Group label="Recently added" icon={faCircleArrowDown}>
            {data.arrivals.length ? (
              <ul style={bareList}>
                {data.arrivals.map((item) => (
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
            <Anchor
              className={styles.panelLink}
              component={Link}
              to="/history/series"
              size="sm"
              onClick={remember}
            >
              Open History
            </Anchor>
          </Group>
        )}

        {data && data.attention.availability !== "available" && (
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
            the incomplete-sources copy silently dropped the overflow count. */}
        {data &&
          data.attention.availability === "available" &&
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
                  and {data.attention.items.length - PANEL_LIMIT} more to review
                </Text>
              )}
            </Group>
          )}

        {data && data.onboarding.availability !== "available" && (
          <Group label="Optional setup" icon={faSliders}>
            <Text size="sm">Optional setup state could not be read.</Text>
          </Group>
        )}

        {data &&
          data.onboarding.availability === "available" &&
          data.onboarding.items.length > 0 && (
            <Group label="Optional setup" icon={faSliders}>
              <ul style={bareList}>
                {data.onboarding.items.map((item) => (
                  <li key={item.id} style={{ marginBottom: 10 }}>
                    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                      {item.summary}
                    </Text>
                    <Anchor
                      className={styles.panelLink}
                      component={Link}
                      to={item.target}
                      size="sm"
                      onClick={remember}
                    >
                      Set this up
                    </Anchor>
                  </li>
                ))}
              </ul>
            </Group>
          )}

        <Group label="Local pages" icon={faCompass}>
          <Anchor
            className={styles.panelLink}
            component={Link}
            to="/system/tasks"
            onClick={remember}
          >
            Activity
          </Anchor>
          <Anchor
            className={styles.panelLink}
            component={Link}
            to="/settings/connections"
            onClick={remember}
          >
            Library connections
          </Anchor>
          {/* An attention item already offers this destination, named for the
              actual problem. Repeating it here as a generic link makes the
              reader wonder whether the two do different things, and costs the
              recovery action its specificity. */}
          {!attentionTargets.has("/system/providers") && (
            <Anchor
              className={styles.panelLink}
              component={Link}
              to="/system/providers"
              onClick={remember}
            >
              Provider status
            </Anchor>
          )}
        </Group>
      </details>
    </aside>
  );
}
