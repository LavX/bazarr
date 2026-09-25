import type { ReactNode } from "react";
import { useState } from "react";
import { Link } from "react-router";
import {
  faArrowRight,
  faClosedCaptioning,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemSettings } from "@/apis/hooks";
import { summaryUnreadable, useDiscoverSummary } from "@/apis/hooks/discover";
import type { DiscoverArrival } from "@/types/discover";
import { readableTime } from "./feedText";
import styles from "./Discover.module.scss";

function destination(item: DiscoverArrival) {
  if (item.kind === "translation") return null;
  if (item.kind === "sports") {
    // A sports arrival names its league, and the league page needs the owning
    // instance to find it.
    return item.library_id
      ? `/sports/${item.library_id}?instance=${item.arr_instance_id}`
      : "/history/sports";
  }
  const category = item.kind === "movie" ? "movies" : "series";
  return item.library_id
    ? `/${category}/${item.library_id}`
    : `/history/${category}`;
}

function ArrivalLink({
  item,
  children,
}: {
  item: DiscoverArrival;
  children: ReactNode;
}) {
  const to = destination(item);
  return to ? (
    <Link to={to} className={styles.libraryArrival}>
      {children}
    </Link>
  ) : (
    <div className={styles.libraryArrival}>{children}</div>
  );
}

/**
 * The compact cover slot for one arrival.
 *
 * The feed tiles next door carry a labelled placeholder that has no room in a
 * 64px slot, so this one falls back to the same caption glyph it sits on. A
 * failure is held in state rather than written onto the node's style, because
 * the list re-renders as the summary refreshes and a hidden node would
 * otherwise survive into the next item that reuses it.
 */
function ArrivalArtwork({ src }: { src: string | null }) {
  const [failed, setFailed] = useState(false);
  return (
    <span className={styles.libraryArtwork} aria-hidden="true">
      <FontAwesomeIcon icon={faClosedCaptioning} />
      {src && !failed && (
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      )}
    </span>
  );
}

/**
 * "hu:hi" as a reader's words.
 *
 * The stored form appends its attributes after a colon, and rendering them raw
 * put an unexplained lowercase "hi" beside the language with the same
 * separator the page uses between unrelated facts. Bracketing keeps the
 * attribute attached to the language it belongs to.
 */
const VARIANT_NAMES: Record<string, string> = {
  hi: "HI",
  forced: "Forced",
};

function languageName(language: string | null) {
  if (!language) return "Subtitle fetched";
  const [code, ...variants] = language.split(":");
  let name = code;
  try {
    name = new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return language;
  }
  if (!variants.length) return name;
  return `${name} (${variants
    .map((variant) => VARIANT_NAMES[variant] ?? variant)
    .join(", ")})`;
}

export default function LibraryActivity() {
  const summary = useDiscoverSummary();
  const settings = useSystemSettings();
  const data = summary.data;
  const librarySetup = data?.onboarding?.items?.find(
    (item) => item.id === "library",
  );
  const arrivals = data?.arrivals ?? [];
  // Every component here is read as optional: a body that answered 200 without
  // them is unavailable, which is the same thing this strip already says about
  // a failed read.
  const unavailable =
    summaryUnreadable(summary) ||
    (data !== undefined && data.arrivals_status?.availability !== "available");
  const historyCategories = [
    ...(settings.data?.general.use_sonarr ||
    arrivals.some((item) => item.kind === "episode")
      ? [{ path: "series", label: "Series history" }]
      : []),
    ...(settings.data?.general.use_radarr ||
    arrivals.some((item) => item.kind === "movie")
      ? [{ path: "movies", label: "Movie history" }]
      : []),
  ];
  return (
    <section
      className={styles.libraryActivity}
      aria-labelledby="library-activity-title"
    >
      <div className={styles.libraryHeading}>
        <div>
          {/* The hero above owns the name "Your library"; this strip is the
              narrower fact of what arrived most recently, and saying both
              would leave two headings claiming the same section. */}
          <h2 id="library-activity-title">Recently fetched</h2>
        </div>
        <div className={styles.libraryHistory}>
          {historyCategories.map(({ path, label }) => (
            <Link key={path} to={`/history/${path}`}>
              {label} <FontAwesomeIcon icon={faArrowRight} />
            </Link>
          ))}
        </div>
      </div>
      {arrivals.length > 0 ? (
        <ul className={styles.libraryArrivals}>
          {arrivals.map((item) => (
            <li key={item.event_id}>
              <ArrivalLink item={item}>
                <ArrivalArtwork
                  key={item.poster_url}
                  src={item.poster_url ?? null}
                />
                <span className={styles.libraryArrivalCopy}>
                  <strong>{item.title ?? "Translated subtitles"}</strong>
                  {item.season !== null && item.episode !== null && (
                    <span>
                      S{String(item.season).padStart(2, "0")}E
                      {String(item.episode).padStart(2, "0")}
                      {item.episode_title ? ` · ${item.episode_title}` : ""}
                    </span>
                  )}
                  <span className={styles.libraryLanguage}>
                    <FontAwesomeIcon icon={faClosedCaptioning} />{" "}
                    {/* Every language fetched for this title, so two languages
                        of one episode read as one arrival in two languages
                        rather than as the same card rendered twice. */}
                    {(item.languages?.length ? item.languages : [item.language])
                      .map(languageName)
                      .join(", ")}
                  </span>
                  {item.timestamp && (
                    <time dateTime={item.timestamp}>
                      {readableTime(item.timestamp)}
                    </time>
                  )}
                  {item.instance_name && <span>{item.instance_name}</span>}
                </span>
              </ArrivalLink>
            </li>
          ))}
        </ul>
      ) : (
        <div className={styles.libraryEmpty}>
          <FontAwesomeIcon icon={faClosedCaptioning} />
          <p>
            {summary.isPending
              ? "Loading your library activity…"
              : unavailable
                ? "Library activity is temporarily unavailable."
                : librarySetup
                  ? "Connect your library to see your movies, series and latest subtitles here."
                  : "Your next fetched subtitles will appear here."}
          </p>
          {unavailable ? (
            <button type="button" onClick={() => void summary.refetch()}>
              Try again
            </button>
          ) : (
            librarySetup && (
              <Link to={librarySetup.target}>
                Connect library <FontAwesomeIcon icon={faArrowRight} />
              </Link>
            )
          )}
        </div>
      )}
    </section>
  );
}
