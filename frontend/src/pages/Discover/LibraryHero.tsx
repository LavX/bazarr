import type { CSSProperties } from "react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  faArrowRight,
  faCircleCheck,
  faClosedCaptioning,
  faFilm,
  faLayerGroup,
  faSpinner,
  faTriangleExclamation,
  faTrophy,
  faTv,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  summaryUnreadable,
  useDiscoverSummary,
  useSportsWantedCount,
} from "@/apis/hooks/discover";
import type { DiscoverSummary } from "@/types/discover";
import { readableTime } from "./feedText";
import styles from "./Discover.module.scss";

/**
 * The reader's own library, given the same weight as the global catalog.
 *
 * The page used to open with small local panels and only reach a full-width
 * hero once it crossed into TMDB's feeds, which read as though the library were
 * a preamble to somebody else's catalogue. This is the same treatment applied
 * to the half of the page that is actually theirs, so the two halves are
 * plainly siblings: their library above the line, the world below it.
 */

/**
 * One counted figure.
 *
 * A count the server could not read is said to be unknown rather than drawn as
 * a zero: an empty library and an unreadable one are different facts, and only
 * one of them is reassuring.
 */
function Figure({
  icon,
  value,
  label,
}: {
  icon: typeof faTv;
  value: number | null | undefined;
  label: string;
}) {
  return (
    <div className={styles.heroStat} data-unknown={value == null}>
      <FontAwesomeIcon icon={icon} aria-hidden="true" />
      <strong>{value == null ? "Unknown" : value.toLocaleString()}</strong>
      <span>{label}</span>
    </div>
  );
}

type Status = {
  tone: "busy" | "quiet" | "attention" | "unknown";
  icon: typeof faSpinner;
  line: string;
  /** When an idle install last fetched something, as the server sent it. */
  fetchedAt?: string;
};

const UNREADABLE: Status = {
  tone: "unknown",
  icon: faTriangleExclamation,
  line: "Local activity could not be read.",
};

/**
 * What this Bazarr is doing right now, in one sentence it can stand behind.
 *
 * Read as a partial summary on purpose. A failed read is not the same as a
 * read that has not happened yet, and a body that arrived without the
 * components this sentence is built from is a failed read however healthy its
 * status code was.
 */
function describe(
  data: Partial<DiscoverSummary> | undefined,
  failed: boolean,
): Status {
  if (failed) return UNREADABLE;
  if (!data)
    return { tone: "unknown", icon: faSpinner, line: "Reading local status" };
  const { activity, attention } = data;
  if (!activity || activity.availability !== "available") return UNREADABLE;
  const running = activity.running_count ?? null;
  const queued = activity.queued_count ?? null;
  if (running === null || queued === null) return UNREADABLE;
  if (running > 0) {
    // Name the work rather than counting it: "Translating Northern Light" is
    // what the reader came to check, and a bare "1 running" is not.
    const featured = activity.running?.[0];
    const rest = running - 1;
    return {
      tone: "busy",
      icon: faSpinner,
      line: featured
        ? `${featured.name}${rest > 0 ? ` and ${rest} more` : ""}`
        : `${running} job${running === 1 ? "" : "s"} running`,
    };
  }
  if (queued > 0)
    return {
      tone: "busy",
      icon: faSpinner,
      line: `${queued} subtitle job${queued === 1 ? "" : "s"} waiting to start`,
    };
  const attentionItems =
    attention?.availability === "available" ? (attention.items ?? []) : [];
  if (attentionItems.length > 0)
    return {
      tone: "attention",
      icon: faTriangleExclamation,
      line: `${attentionItems.length} thing${
        attentionItems.length === 1 ? "" : "s"
      } need attention`,
    };
  // "Nothing running" is an absence, and an absence is a poor thing to lead a
  // page with. An idle install's most interesting status is when it last did
  // something, which is a fact rather than the lack of one.
  const last = data.arrivals?.find((item) => item.timestamp);
  return {
    tone: "quiet",
    icon: faCircleCheck,
    line: "Subtitle jobs idle",
    fetchedAt: last?.timestamp ?? undefined,
  };
}

/** How long one cover holds before the next arrival takes its turn. */
const COVER_SECONDS = 10;

export default function LibraryHero() {
  const summary = useDiscoverSummary();
  // Several covers can fail independently, so this is the set of the ones that
  // did rather than the last one: a single slot would let a broken image be
  // retried on every turn of the rotation.
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set());
  const [turn, setTurn] = useState(0);
  const data = summary.data;
  const library = data?.library;
  const status = describe(data, summaryUnreadable(summary));
  // The things this install actually fetched, which are the only images on the
  // page unambiguously about this library. Deduplicated because one title can
  // arrive more than once and would otherwise hold the frame twice as long.
  const covers = useMemo(() => {
    const seen = new Set<string>();
    for (const item of data?.arrivals ?? []) {
      if (item.backdrop_url && !failed.has(item.backdrop_url)) {
        seen.add(item.backdrop_url);
      }
    }
    return [...seen];
  }, [data?.arrivals, failed]);
  useEffect(() => {
    // Nothing to rotate through, so nothing to schedule.
    if (covers.length < 2) return;
    const timer = window.setInterval(
      () => setTurn((current) => current + 1),
      COVER_SECONDS * 1000,
    );
    return () => window.clearInterval(timer);
  }, [covers.length]);
  const backdrop = covers.length ? covers[turn % covers.length] : null;
  // Fetch the next one while this one is still showing, so its turn begins on
  // a painted image rather than on the empty panel behind it.
  useEffect(() => {
    if (covers.length < 2) return;
    const next = new Image();
    next.src = covers[(turn + 1) % covers.length];
  }, [covers, turn]);
  const wanted = data?.wanted;
  const sportsWanted = useSportsWantedCount();
  // Media items, never language requirements. One episode missing Hungarian
  // and English is one episode needing subtitles but two requirements, and
  // labelling the larger figure "episodes" overstated the work by a third.
  const missing = [
    {
      count: wanted?.episode_media_count ?? 0,
      to: "/wanted/series",
      label: (count: number) =>
        `${count} episode${count === 1 ? " needs" : "s need"} subtitles`,
    },
    {
      count: wanted?.movie_media_count ?? 0,
      to: "/wanted/movies",
      label: (count: number) =>
        `${count} movie${count === 1 ? " needs" : "s need"} subtitles`,
    },
    {
      // Sportarr counts itself, so this is absent rather than zero wherever it
      // is switched off or not in this build, and the action never appears.
      count: sportsWanted.data ?? 0,
      to: "/wanted/sports",
      label: (count: number) =>
        `${count} sports event${count === 1 ? " needs" : "s need"} subtitles`,
    },
  ]
    // The larger queue leads, because it is the one worth opening first.
    .filter((entry) => entry.count > 0)
    .sort((left, right) => right.count - left.count)
    .map((entry) => ({ to: entry.to, label: entry.label(entry.count) }));
  const scheduled = data?.activity?.scheduled_count ?? null;
  // A job with no further run reports exactly "Never", and everything else is
  // an upcoming run. Testing for a leading "in" instead looked equivalent but
  // rejected half the vocabulary the server actually produces ("now", "today",
  // "tomorrow", "next week"), so the soonest jobs were the ones that could
  // never be named and a later one got labelled next in their place.
  const next = data?.activity?.scheduled?.find(
    (job) => (job.next_run_in ?? "Never") !== "Never",
  );

  return (
    <section
      className={`${styles.stage} ${styles.libraryStage}`}
      aria-labelledby="discover-library-hero-title"
      data-artwork={backdrop ? "true" : "false"}
      style={
        backdrop
          ? ({ "--feature-backdrop": `url("${backdrop}")` } as CSSProperties)
          : undefined
      }
    >
      <article
        className={`${styles.feature} ${styles.libraryFeature}`}
        data-artwork={backdrop ? "true" : "false"}
      >
        {backdrop && (
          <img
            // Keyed by source so a new cover mounts as its own element and
            // replays the panel's entrance fade instead of swapping in place.
            key={backdrop}
            src={backdrop}
            alt=""
            loading="eager"
            decoding="async"
            onError={() =>
              setFailed((current) => new Set(current).add(backdrop))
            }
          />
        )}
        <div className={styles.featureCopy}>
          {/* Heading and status share a row, and the schedule note rides with
              the actions. Stacked as five separate blocks this panel ran past
              430px, which pushed the first actual missing-subtitle row below
              the fold on a laptop: a lot of height spent describing the
              library rather than showing the work. */}
          <div className={styles.heroTop}>
            <h2 id="discover-library-hero-title">Your library</h2>
            <p className={styles.heroStatus} data-tone={status.tone}>
              <FontAwesomeIcon
                icon={status.icon}
                aria-hidden="true"
                spin={status.tone === "busy"}
              />
              {/* The state and the time it last fetched are separate elements
                  because they are worth different things to a reader. At 400px
                  the whole sentence does not fit, and cutting its end cut the
                  time, which is the one part anybody opens this panel to read.
                  So the time drops to a second line whole instead, and only
                  the state (or a long job name) is ever elided. */}
              <span className={styles.heroStatusText}>
                <span className={styles.heroStatusLead}>{status.line}</span>
                {status.fetchedAt && (
                  <>
                    {" "}
                    <span className={styles.heroStatusWhen}>
                      <span className={styles.heroStatusSeparator}>·</span> last
                      fetched{" "}
                      <time dateTime={status.fetchedAt}>
                        {readableTime(status.fetchedAt)}
                      </time>
                    </span>
                  </>
                )}
              </span>
            </p>
          </div>
          <div className={styles.heroStats}>
            <Figure icon={faTv} value={library?.series} label="Series" />
            <Figure icon={faFilm} value={library?.movies} label="Movies" />
            <Figure
              icon={faLayerGroup}
              value={library?.episodes}
              label="Episodes"
            />
            {/* Only where Sportarr is switched on. The count is absent rather
                than zero otherwise, so a reader without it is not shown a tile
                inviting them to look for a feature they do not have. */}
            {library?.sports_events !== undefined && (
              <Figure
                icon={faTrophy}
                value={library.sports_events}
                label="Sports events"
              />
            )}
            <Figure
              icon={faClosedCaptioning}
              value={library?.subtitles_fetched}
              label="Subtitles fetched, all time"
            />
          </div>
          <div className={styles.heroActions}>
            {/* Missing episodes, movies and sports live on separate pages, so
                a single link claiming to open "what is missing" would leave the
                others unreachable and unmentioned. Each kind is named with its
                own count and opens its own page, and every one gets the same
                control: they are the same kind of thing, a queue of work with a
                page behind it. Giving one the button and the rest a quiet link
                implied a ranking that only reflected which number happened to
                be largest. */}
            {missing.map((entry) => (
              <Link key={entry.to} to={entry.to} className={styles.heroCta}>
                {entry.label}{" "}
                <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" />
              </Link>
            ))}
            {/* The state of the queue, then the way into it. Putting the link
                first made it a heading for a fact it does not describe. */}
            <span className={styles.heroSchedule}>
              {scheduled === null
                ? "Scheduled work could not be read."
                : next
                  ? `Next: ${next.name} ${next.next_run_in}`
                  : `${scheduled} scheduled job${scheduled === 1 ? "" : "s"}`}
            </span>
            <Link to="/system/tasks">
              All jobs{" "}
              <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" />
            </Link>
          </div>
        </div>
      </article>
    </section>
  );
}
