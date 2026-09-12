import { Button, Group, Text, Title } from "@mantine/core";
import {
  faChevronDown,
  faDownload,
  faEye,
  faFileLines,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscover } from "@/contexts/Discover";
import type {
  DiscoverCompatibility,
  DiscoverCopyCompatibility,
  DiscoverSearchSnapshot,
} from "@/types/discover";
import { copyFacts, copyOwner, copyRelease } from "./LocalCopyPicker";
import SubtitlePreview, { previewSourceId } from "./SubtitlePreview";
import styles from "./Discover.module.scss";

// Transport field name paired with the words a reader uses for it. A list of
// literals rather than an object, so the order is the reading order and the
// transport names stay out of the identifier namespace.
const attributeNames: [keyof DiscoverCopyCompatibility, string][] = [
  ["source", "source"],
  ["resolution", "resolution"],
  ["video_codec", "video codec"],
  ["audio_codec", "audio codec"],
  ["release_group", "release group"],
  ["edition", "edition"],
];

function attributes(
  compatibility: DiscoverCopyCompatibility,
  state: DiscoverCompatibility,
) {
  return attributeNames
    .filter(([key]) => compatibility[key] === state)
    .map(([, label]) => label)
    .join(", ");
}

export default function SubtitleResults({
  snapshot,
}: {
  snapshot: DiscoverSearchSnapshot;
}) {
  const {
    state,
    downloadSubtitle,
    previewSubtitle,
    closePreview,
    searchAgain,
  } = useDiscover();
  const feedback = state.download;
  const context = feedback?.context;
  const target =
    context?.mode === "release"
      ? `${context.query} (unverified release query)`
      : context
        ? `${context.title ?? context.imdb_id}${context.media_type === "episode" ? ` S${String(context.season).padStart(2, "0")} E${String(context.episode).padStart(2, "0")}` : context.year ? ` (${context.year})` : ""}`
        : "";
  const scope =
    feedback?.row.scope === "forced"
      ? "Forced"
      : feedback?.row.scope === "full"
        ? "Full subtitles"
        : "Scope unknown";
  const identity = feedback
    ? `${target} · ${context?.language} · ${scope} · ${feedback.row.provider} · ${feedback.row.release ?? "Release information unavailable"}`
    : "";
  const isPreviewed = (id: string, searchId: string) =>
    state.preview?.row.id === id && state.preview.row.search_id === searchId;
  const copy =
    snapshot.context.mode === "release" ? undefined : snapshot.context.copy;
  return (
    <>
      <ul className={styles.results} aria-label="Subtitle results">
        {snapshot.results.map((row) => (
          <li key={row.id}>
            <article
              className={styles.result}
              tabIndex={-1}
              data-previewed={isPreviewed(row.id, row.search_id)}
            >
              <Group
                justify="space-between"
                align="start"
                wrap="nowrap"
                className={styles.resultHeader}
              >
                <span className={styles.resultDocument} aria-hidden="true">
                  <FontAwesomeIcon icon={faFileLines} />
                </span>
                <details className={styles.resultDetails}>
                  <summary
                    className={styles.release}
                    aria-label={`Match evidence for ${row.release ?? row.provider}`}
                  >
                    <Title order={3} size="h4" title={row.release ?? undefined}>
                      {row.release ?? "Release information unavailable"}
                    </Title>
                    <span className={styles.resultAttributes}>
                      <span className={styles.resultProvider}>
                        {row.provider}
                      </span>
                      <span>SRT</span>
                      {row.scope === "forced" && <span>Forced</span>}
                      {row.hearing_impaired && (
                        <span>Hearing-impaired cues</span>
                      )}
                      {row.stale && <span>Previous result</span>}
                    </span>
                    <span className={styles.matchDetailsHint}>
                      Match details <FontAwesomeIcon icon={faChevronDown} />
                    </span>
                  </summary>
                  <dl>
                    <div>
                      <dt>Language</dt>
                      <dd>
                        {row.language}
                        {row.language_variant
                          ? ` · ${row.language_variant}`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>Scope</dt>
                      <dd>
                        {row.scope === "unknown"
                          ? "Scope unknown"
                          : row.scope === "forced"
                            ? "Forced"
                            : "Full subtitles"}
                      </dd>
                    </div>
                    <div>
                      <dt>Hearing-impaired cues</dt>
                      <dd>
                        {row.hearing_impaired === null
                          ? "Unknown"
                          : row.hearing_impaired
                            ? "Included"
                            : "Not included"}
                      </dd>
                    </div>
                    {row.uploader && (
                      <div>
                        <dt>Uploader</dt>
                        <dd>{row.uploader}</dd>
                      </div>
                    )}
                    {copy && (
                      <div>
                        <dt>Search context</dt>
                        <dd>
                          Search context: {copyOwner(copy)} ·{" "}
                          {copyRelease(copy)}
                          {copyFacts(copy) ? ` · ${copyFacts(copy)}` : ""}
                        </dd>
                      </div>
                    )}
                    <div>
                      <dt>Known matches</dt>
                      <dd>
                        {row.matches?.length
                          ? row.matches.join(", ").replaceAll("_", " ")
                          : "Unknown"}
                      </dd>
                    </div>
                    <div>
                      <dt>
                        {snapshot.context.mode === "release"
                          ? "Compatibility"
                          : "Title compatibility score"}
                      </dt>
                      <dd>
                        {row.compatibility_score === null
                          ? "Unknown"
                          : `${row.compatibility_score} / ${row.compatibility_score_max}`}
                      </dd>
                    </div>
                    {row.copy_compatibility && (
                      <>
                        <div>
                          <dt>Matches this copy</dt>
                          <dd>
                            {attributes(row.copy_compatibility, "match") ||
                              "Nothing stated by both"}
                          </dd>
                        </div>
                        <div>
                          <dt>Conflicts with this copy</dt>
                          <dd>
                            {attributes(row.copy_compatibility, "conflict") ||
                              "None stated"}
                          </dd>
                        </div>
                        <div>
                          <dt>Unknown for this copy</dt>
                          <dd>
                            {attributes(row.copy_compatibility, "unknown") ||
                              "None"}
                          </dd>
                        </div>
                      </>
                    )}
                    <div>
                      <dt>Provider rating</dt>
                      <dd>{row.rating ?? "Unknown"}</dd>
                    </div>
                    <div>
                      <dt>Checked</dt>
                      <dd>
                        <time dateTime={row.checked_at}>
                          {new Date(row.checked_at).toLocaleString()}
                        </time>
                      </dd>
                    </div>
                  </dl>
                  <Text size="sm" c="dimmed">
                    {snapshot.context.mode === "release"
                      ? "Release queries do not verify title, episode identity or subtitle timing for a video file."
                      : copy
                        ? "Release details from this copy are search context. They do not verify subtitle synchronization with the file you play."
                        : "Title matches do not verify subtitle timing for a video file."}
                  </Text>
                </details>
                <Group gap={0} wrap="nowrap" className={styles.resultActions}>
                  <Button
                    id={previewSourceId(row)}
                    variant="default"
                    className={styles.resultPreview}
                    leftSection={<FontAwesomeIcon icon={faEye} />}
                    aria-expanded={isPreviewed(row.id, row.search_id)}
                    aria-controls={
                      isPreviewed(row.id, row.search_id)
                        ? "discover-subtitle-preview"
                        : undefined
                    }
                    mih={44}
                    disabled={state.retiredResultIds.includes(row.id)}
                    onClick={() =>
                      isPreviewed(row.id, row.search_id)
                        ? closePreview()
                        : void previewSubtitle(row)
                    }
                  >
                    {isPreviewed(row.id, row.search_id)
                      ? "Close preview"
                      : "Preview"}
                  </Button>
                  <Button
                    variant="light"
                    className={styles.resultDownload}
                    leftSection={<FontAwesomeIcon icon={faDownload} />}
                    aria-label="Download SRT"
                    title="Download SRT to your device"
                    loading={
                      feedback?.status === "pending" &&
                      feedback.row.id === row.id
                    }
                    disabled={
                      state.retiredResultIds.includes(row.id) ||
                      feedback?.status === "pending"
                    }
                    onClick={() => void downloadSubtitle(row)}
                  >
                    Download
                  </Button>
                </Group>
              </Group>
            </article>
            {state.preview && isPreviewed(row.id, row.search_id) && (
              <SubtitlePreview
                preview={state.preview}
                searching={state.status === "searching"}
                close={closePreview}
                retry={previewSubtitle}
                searchAgain={searchAgain}
              />
            )}
          </li>
        ))}
      </ul>
      {feedback && (
        <div
          role="status"
          aria-live="polite"
          className={styles.downloadFeedback}
        >
          <Text>
            {feedback.status === "pending"
              ? `Preparing download for ${identity}.`
              : feedback.status === "started"
                ? `Download started for ${identity}.`
                : feedback.status === "expired"
                  ? `This result has expired: ${identity}. Search again for the same selection.`
                  : `Download failed for ${identity}. Retry this result or choose another subtitle.`}
          </Text>
          {feedback.status === "expired" && (
            <Button
              variant="filled"
              color="brand"
              mt="sm"
              disabled={state.status === "searching"}
              aria-label="Search again for expired result"
              onClick={() => void searchAgain()}
            >
              Search again
            </Button>
          )}
        </div>
      )}
    </>
  );
}
