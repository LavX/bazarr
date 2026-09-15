import { Link } from "react-router";
import { faArrowRight, faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useEpisodeSubtitleModification,
  useMovieSubtitleModification,
} from "@/apis/hooks";
import { useDiscoverSummary, useWantedPreview } from "@/apis/hooks/discover";
import styles from "./Discover.module.scss";

/** Per group, so neither kind can crowd the other out of the preview. */
const PER_GROUP = 3;

type Entry = {
  key: string;
  title: string;
  detail: string | null;
  to: string;
  missing: Subtitle[];
  /**
   * Absent where no single control can both search and download.
   *
   * Sportarr's per-event endpoint returns candidates for a person to choose
   * between, which is not what the series and movie pills do. Rendering the
   * same button for it would promise an action that does not happen, so those
   * rows show what is missing and link to where it can be acted on.
   */
  search?: (language: Subtitle) => Promise<unknown>;
};

type Group = {
  id: string;
  heading: string;
  to: string;
  linkLabel: string;
  entries: Entry[];
};

/**
 * Sonarr reports an episode as "1x1" and the history strip renders "S01E01".
 * One page should not spell the same thing two ways, so both go through here.
 */
function languageFromCode(code: string): Subtitle {
  let name = code;
  try {
    name = new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    name = code;
  }
  return {
    name,
    code2: code as Language.CodeType,
    hi: false,
    forced: false,
    path: null,
  };
}

function episodeLabel(value: string): string {
  const match = /^(\d+)x(\d+)$/.exec(value.trim());
  if (!match) return value;
  return `S${match[1].padStart(2, "0")}E${match[2].padStart(2, "0")}`;
}

/**
 * The head of the wanted queue, where the reader is already about to search.
 *
 * Discover exists to run a subtitle search, and the library usually already
 * knows exactly which ones are missing. Each language starts the same provider
 * search the Wanted page starts, so the shortest path from "something is
 * missing" to "go and get it" does not leave the page.
 */
export default function WantedQueue() {
  const summary = useDiscoverSummary();
  const wanted = useWantedPreview(PER_GROUP);
  const { download: downloadEpisode } = useEpisodeSubtitleModification();
  const { download: downloadMovie } = useMovieSubtitleModification();
  const counts = summary.data?.wanted;
  const pending = downloadEpisode.isPending || downloadMovie.isPending;

  const groups: Group[] = [
    {
      id: "episodes",
      heading: "Episodes",
      to: "/wanted/series",
      linkLabel: "All missing episodes",
      entries: wanted.episodes.slice(0, PER_GROUP).map((item) => ({
        key: `episode:${item.sonarrEpisodeId}`,
        title: item.seriesTitle,
        detail: `${episodeLabel(item.episode_number)} · ${item.episodeTitle}`,
        to: `/series/${item.sonarrSeriesId}`,
        missing: item.missing_subtitles,
        search: (language: Subtitle) =>
          downloadEpisode.mutateAsync({
            seriesId: item.sonarrSeriesId,
            episodeId: item.sonarrEpisodeId,
            arrInstanceId: item.arr_instance_id,
            form: {
              language: language.code2,
              hi: language.hi,
              forced: language.forced,
            },
          }),
      })),
    },
    {
      id: "movies",
      heading: "Movies",
      to: "/wanted/movies",
      linkLabel: "All missing movies",
      entries: wanted.movies.slice(0, PER_GROUP).map((item) => ({
        key: `movie:${item.radarrId}`,
        title: item.title,
        detail: null,
        to: `/movies/${item.radarrId}`,
        missing: item.missing_subtitles,
        search: (language: Subtitle) =>
          downloadMovie.mutateAsync({
            radarrId: item.radarrId,
            arrInstanceId: item.arr_instance_id,
            form: {
              language: language.code2,
              hi: language.hi,
              forced: language.forced,
            },
          }),
      })),
    },
    {
      id: "sports",
      heading: "Sports",
      to: "/wanted/sports",
      linkLabel: "All missing sports",
      entries: wanted.sports.slice(0, PER_GROUP).map((item) => ({
        key: `sports:${item.id}`,
        title: item.title,
        detail: item.partName,
        to: item.league_id === null ? "/sports" : `/sports/${item.league_id}`,
        missing: (item.missing_subtitles ?? []).map(languageFromCode),
      })),
    },
  ].filter((group) => group.entries.length > 0);

  if (!wanted.connected) return null;
  if (!wanted.isPending && !wanted.isError && groups.length === 0) return null;

  return (
    <section className={styles.wanted} aria-labelledby="discover-wanted-title">
      <div className={styles.wantedHeading}>
        <div>
          <h2 id="discover-wanted-title">Still missing</h2>
          {/* Two units, both named: an item can need several languages, so the
              larger figure is subtitles and the smaller one is media. */}
          {counts?.requirements != null && counts.media_count != null && (
            <p>
              {counts.requirements} subtitle
              {counts.requirements === 1 ? "" : "s"} missing across{" "}
              {counts.media_count} episode
              {counts.media_count === 1 ? "" : "s"} and movies
            </p>
          )}
        </div>
      </div>
      {wanted.isError ? (
        <p className={styles.wantedEmpty}>The wanted list could not be read.</p>
      ) : wanted.isPending ? (
        <p className={styles.wantedEmpty}>Reading what is still missing…</p>
      ) : (
        <div className={styles.wantedGroups}>
          {/* Two independent previews, said out loud. Interleaved into one grid
              the columns happened to sort themselves by kind, which looked like
              a structure nobody had labelled and implied a relationship between
              rows that share nothing. */}
          {groups.map((group) => (
            <section
              key={group.id}
              className={styles.wantedGroup}
              aria-labelledby={`discover-wanted-${group.id}`}
            >
              <div className={styles.wantedGroupHead}>
                <h3 id={`discover-wanted-${group.id}`}>{group.heading}</h3>
                <Link to={group.to}>
                  {group.linkLabel} <FontAwesomeIcon icon={faArrowRight} />
                </Link>
              </div>
              <ul className={styles.wantedList}>
                {group.entries.map((entry) => (
                  <li key={entry.key}>
                    <Link to={entry.to} className={styles.wantedTitle}>
                      <strong>{entry.title}</strong>
                      {entry.detail && <span>{entry.detail}</span>}
                    </Link>
                    <span className={styles.wantedLanguages}>
                      {entry.missing.map((language) =>
                        entry.search === undefined ? (
                          <span
                            key={`${language.code2}:${language.hi}:${language.forced}`}
                            className={styles.wantedLanguageBadge}
                          >
                            {language.name}
                          </span>
                        ) : (
                          <button
                            type="button"
                            key={`${language.code2}:${language.hi}:${language.forced}`}
                            disabled={pending}
                            // A bare language name says what is missing, not what
                            // pressing it does, and the same control could as
                            // easily be a filter or a badge. The verb is the
                            // point; the media name keeps the accessible name
                            // distinguishable from every other pill on the page.
                            title={
                              language.hi
                                ? "Search for hearing impaired subtitles"
                                : language.forced
                                  ? "Search for forced subtitles"
                                  : undefined
                            }
                            aria-label={`Search providers for ${language.name}${
                              language.hi
                                ? " hearing impaired"
                                : language.forced
                                  ? " forced"
                                  : ""
                            } subtitles for ${entry.title}`}
                            onClick={() => void entry.search?.(language)}
                          >
                            <FontAwesomeIcon
                              icon={faSearch}
                              aria-hidden="true"
                            />{" "}
                            Search {language.name}
                            {language.hi
                              ? " (HI)"
                              : language.forced
                                ? " (Forced)"
                                : ""}
                          </button>
                        ),
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </section>
  );
}
