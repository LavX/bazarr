import { Badge, Button, Group, Stack, Text, Title } from "@mantine/core";
import { faDownload } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscover } from "@/contexts/Discover";
import type {
  DiscoverCompatibility,
  DiscoverCopyCompatibility,
  DiscoverSearchSnapshot,
} from "@/types/discover";
import { copyFacts, copyOwner, copyRelease } from "./LocalCopyPicker";
import { previewSourceId } from "./SubtitlePreview";
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
  const { state, downloadSubtitle, previewSubtitle, searchAgain } =
    useDiscover();
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
  const copy =
    snapshot.context.mode === "release" ? undefined : snapshot.context.copy;
  return (
    <>
      {copy && (
        <Stack gap={4} mb="md">
          <Text size="sm">
            Search context: {copyOwner(copy)} · Local {copy.media_type}{" "}
            {copy.local_id} · {copyRelease(copy)}
            {copyFacts(copy) ? ` · ${copyFacts(copy)}` : ""}
          </Text>
          <Text size="sm" c="dimmed">
            Release details from this copy are search context. They do not
            verify subtitle synchronization with the file you play.
          </Text>
        </Stack>
      )}
      <ul className={styles.results} aria-label="Subtitle results">
        {snapshot.results.map((row) => (
          <li key={row.id}>
            <article className={styles.result} tabIndex={-1}>
              <Group justify="space-between" align="start" wrap="wrap">
                <div className={styles.release}>
                  <Title order={3} size="h4">
                    {row.release ?? "Release information unavailable"}
                  </Title>
                  <Text size="sm" c="dimmed">
                    {row.provider}
                    {row.uploader ? ` · ${row.uploader}` : ""}
                  </Text>
                </div>
                {row.stale && (
                  <Badge color="yellow" variant="light">
                    Previous result
                  </Badge>
                )}
              </Group>
              <Group gap="xs" mt="sm" wrap="wrap">
                <Badge variant="light">{row.language}</Badge>
                <Badge variant="outline" color="gray">
                  {row.scope === "unknown"
                    ? "Scope unknown"
                    : row.scope === "forced"
                      ? "Forced"
                      : "Full subtitles"}
                </Badge>
                <Badge variant="outline" color="gray">
                  {row.hearing_impaired === null
                    ? "HI unknown"
                    : row.hearing_impaired
                      ? "Hearing impaired"
                      : "No HI"}
                </Badge>
                {row.language_variant && (
                  <Badge variant="outline" color="gray">
                    {row.language_variant}
                  </Badge>
                )}
              </Group>
              <details className={styles.evidence}>
                <summary>Match evidence</summary>
                <dl>
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
                      ? "A matching source, resolution or provider rating does not certify that these subtitles are synchronized with your copy."
                      : "Title matches do not verify subtitle timing for a video file."}
                </Text>
              </details>
              <Group className={styles.downloadAction} justify="space-between">
                <Text size="sm" c="dimmed">
                  Download to your device.
                </Text>
                <Group gap="xs" wrap="wrap">
                  <Button
                    id={previewSourceId(row)}
                    variant="default"
                    mih={44}
                    disabled={state.retiredResultIds.includes(row.id)}
                    onClick={() => void previewSubtitle(row)}
                  >
                    Preview
                  </Button>
                  <Button
                    variant="filled"
                    mih={44}
                    color="brand"
                    leftSection={<FontAwesomeIcon icon={faDownload} />}
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
                    Download SRT
                  </Button>
                </Group>
              </Group>
            </article>
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
