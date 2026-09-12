import { useEffect, useRef } from "react";
import { ActionIcon, Button, Group, Stack, Text } from "@mantine/core";
import { faClosedCaptioning, faXmark } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type {
  DiscoverPreviewFeedback,
  DiscoverSubtitleResult,
} from "@/types/discover";
import { renderSubtitleHtml } from "@/utilities/subtitleText";
import styles from "./Discover.module.scss";

export function previewSourceId(row: DiscoverSubtitleResult) {
  return `discover-preview-${encodeURIComponent(JSON.stringify([row.search_id, row.id]))}`;
}

interface PreviewProps {
  preview: DiscoverPreviewFeedback;
  searching: boolean;
  close: () => void;
  retry: (row: DiscoverSubtitleResult) => Promise<void>;
  searchAgain: () => Promise<void>;
}

function timestamp(milliseconds: number) {
  const seconds = Math.floor(milliseconds / 1000);
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}.${String(milliseconds % 1000).padStart(3, "0")}`;
}

export default function SubtitlePreview({
  preview,
  searching,
  close,
  retry,
  searchAgain,
}: PreviewProps) {
  const { context, row, data, status } = preview;
  const target =
    context.mode === "release"
      ? `${context.query} (unverified release query)`
      : `${context.title ?? context.imdb_id}${context.media_type === "episode" ? ` S${String(context.season).padStart(2, "0")} E${String(context.episode).padStart(2, "0")}` : context.year ? ` (${context.year})` : ""}`;
  const heading = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    heading.current
      ?.closest("section")
      ?.scrollIntoView?.({ block: "start", behavior: "instant" });
  }, [row.id, row.search_id, status]);
  const dismiss = () => {
    close();
    const button = document.getElementById(previewSourceId(row));
    const opener =
      button instanceof HTMLButtonElement && button.disabled
        ? button.closest("article")
        : button;
    opener?.focus({ preventScroll: true });
  };
  return (
    <Stack
      component="section"
      id="discover-subtitle-preview"
      role="region"
      aria-labelledby="discover-preview-heading"
      aria-describedby="discover-preview-identity"
      gap="lg"
      className={styles.previewCard}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          dismiss();
        }
      }}
    >
      <Group
        justify="space-between"
        wrap="nowrap"
        className={styles.previewHeading}
      >
        <div className={styles.previewHeadingText}>
          <Text
            id="discover-preview-heading"
            ref={heading}
            tabIndex={-1}
            fw={650}
          >
            <FontAwesomeIcon icon={faClosedCaptioning} aria-hidden="true" />{" "}
            Text preview
          </Text>
          <Text className={styles.previewSource}>{row.provider} · SRT</Text>
        </div>
        <ActionIcon
          variant="subtle"
          color="gray"
          size={44}
          aria-label="Close preview"
          onClick={dismiss}
        >
          <FontAwesomeIcon icon={faXmark} />
        </ActionIcon>
      </Group>
      <Text id="discover-preview-identity" className={styles.visuallyHidden}>
        {target} · {row.release ?? "Release information unavailable"} ·{" "}
        {row.language} · {row.provider}
      </Text>
      <Text className={styles.previewRelease} title={row.release ?? undefined}>
        {row.release ?? target}
      </Text>
      {status === "pending" && (
        <Text role="status">Loading subtitle preview...</Text>
      )}
      {status === "failed" && (
        <Stack gap="sm">
          <Text role="alert">
            The provider could not return a valid subtitle for preview. Retry or
            choose another result.
          </Text>
          <Button variant="light" mih={44} onClick={() => void retry(row)}>
            Retry preview
          </Button>
        </Stack>
      )}
      {status === "expired" && (
        <Stack gap="sm">
          <Text role="alert">
            This result has expired. Search again for the same selection.
          </Text>
          <Button
            variant="light"
            mih={44}
            disabled={searching}
            onClick={() => void searchAgain()}
          >
            Search again
          </Button>
        </Stack>
      )}
      {status === "ready" && data && (
        <>
          <div className={styles.previewStats}>
            <span>
              {data.truncated
                ? `${data.cues.length} of ${data.total_cues} cues`
                : `${data.total_cues} cues`}
            </span>
            {data.truncated && <span>Full subtitle included in download</span>}
          </div>
          <ol
            aria-label="Subtitle cues"
            className={styles.cueList}
            tabIndex={0}
          >
            {data.cues.map((cue, index) => (
              <li key={index} className={styles.cueRow}>
                <Text className={styles.cueTime}>
                  <span className={styles.cueNumber}>
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <time
                    title={`${timestamp(cue.start_ms)} to ${timestamp(cue.end_ms)}`}
                  >
                    {timestamp(cue.start_ms).split(".")[0]}
                  </time>
                </Text>
                <Text
                  component="div"
                  className={styles.cueText}
                  dangerouslySetInnerHTML={{
                    __html: renderSubtitleHtml(cue.text),
                  }}
                />
              </li>
            ))}
          </ol>
        </>
      )}
      <Text size="sm" className={styles.previewNote}>
        Text preview only. Timing against your video has not been checked.
      </Text>
    </Stack>
  );
}
