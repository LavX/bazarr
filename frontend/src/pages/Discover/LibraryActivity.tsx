import type { ReactNode } from "react";
import { Link } from "react-router";
import {
  faArrowRight,
  faClosedCaptioning,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemSettings } from "@/apis/hooks";
import { useDiscoverSummary } from "@/apis/hooks/discover";
import type { DiscoverArrival } from "@/types/discover";
import { readableTime } from "./feedText";
import styles from "./Discover.module.scss";

function destination(item: DiscoverArrival) {
  if (item.kind === "translation") return null;
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

function languageName(language: string | null) {
  if (!language) return "Subtitle fetched";
  const [code, ...variants] = language.split(":");
  try {
    return [
      new Intl.DisplayNames(["en"], { type: "language" }).of(code),
      ...variants,
    ].join(" · ");
  } catch {
    return language;
  }
}

export default function LibraryActivity() {
  const summary = useDiscoverSummary();
  const settings = useSystemSettings();
  const data = summary.data;
  const librarySetup = data?.onboarding.items.find(
    (item) => item.id === "library",
  );
  const arrivals = data?.arrivals ?? [];
  const unavailable =
    !summary.isPending &&
    (summary.isError || data?.arrivals_status.availability !== "available");
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
          <h2 id="library-activity-title">Your library</h2>
          {arrivals.length > 0 && <p>Latest subtitles fetched</p>}
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
                <span className={styles.libraryArtwork} aria-hidden="true">
                  <FontAwesomeIcon icon={faClosedCaptioning} />
                  {item.poster_url && (
                    <img
                      src={item.poster_url}
                      alt=""
                      onError={(event) => {
                        event.currentTarget.style.display = "none";
                      }}
                    />
                  )}
                </span>
                <span className={styles.libraryArrivalCopy}>
                  <strong>{item.title ?? "Translated subtitles"}</strong>
                  {item.season !== null && item.episode !== null && (
                    <span>
                      S{String(item.season).padStart(2, "0")} E
                      {String(item.episode).padStart(2, "0")}
                      {item.episode_title ? ` · ${item.episode_title}` : ""}
                    </span>
                  )}
                  <span className={styles.libraryLanguage}>
                    <FontAwesomeIcon icon={faClosedCaptioning} />{" "}
                    {languageName(item.language)}
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
