import { useEffect, useRef } from "react";
import { Badge, Button, Group, Stack, Text } from "@mantine/core";
import { useReducedMotion } from "@mantine/hooks";
import { modals } from "@mantine/modals";
import type {
  DiscoverDownloadFeedback,
  DiscoverPreviewFeedback,
  DiscoverSubtitleResult,
} from "@/types/discover";

const MODAL_ID = "discover-subtitle-preview";

export function previewSourceId(row: DiscoverSubtitleResult) {
  return `discover-preview-${encodeURIComponent(JSON.stringify([row.search_id, row.id]))}`;
}

interface PreviewProps {
  preview: DiscoverPreviewFeedback;
  download: DiscoverDownloadFeedback | null;
  searching: boolean;
  close: () => void;
  retry: (row: DiscoverSubtitleResult) => Promise<void>;
  downloadSubtitle: (row: DiscoverSubtitleResult) => Promise<void>;
  searchAgain: () => Promise<void>;
}

function timestamp(milliseconds: number) {
  const seconds = Math.floor(milliseconds / 1000);
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}.${String(milliseconds % 1000).padStart(3, "0")}`;
}

export default function SubtitlePreview({
  preview,
  download,
  searching,
  close,
  retry,
  downloadSubtitle,
  searchAgain,
}: PreviewProps) {
  const { context, row, data, status } = preview;
  const target =
    context.mode === "release"
      ? `${context.query} (unverified release query)`
      : `${context.title ?? context.imdb_id}${context.media_type === "episode" ? ` S${String(context.season).padStart(2, "0")} E${String(context.episode).padStart(2, "0")}` : context.year ? ` (${context.year})` : ""}`;
  const downloading = download?.status === "pending";
  return (
    <Stack
      gap="lg"
      style={{
        overflowWrap: "anywhere",
        color: "var(--bz-text-primary)",
        minWidth: 0,
      }}
    >
      <div>
        <Text fw={650} size="lg">
          {target}
        </Text>
        <Text mt={4}>{row.release ?? "Release information unavailable"}</Text>
        <Group mt="sm" gap="xs">
          <Badge variant="light">{row.language}</Badge>
          <Badge variant="outline" color="gray">
            {row.scope === "forced"
              ? "Forced"
              : row.scope === "full"
                ? "Full subtitles"
                : "Scope unknown"}
          </Badge>
          <Text size="sm">{row.provider}</Text>
        </Group>
      </div>
      <Text size="sm" c="var(--bz-text-secondary)">
        Cue text does not verify timing for your video. Download uses the same
        subtitle content.
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
          <Text size="sm">
            {data.truncated
              ? `Preview limited to ${data.cues.length} cues. Download contains all ${data.total_cues} complete cues.`
              : `${data.total_cues} ${data.total_cues === 1 ? "cue" : "cues"}.`}
          </Text>
          <ol
            aria-label="Subtitle cues"
            style={{
              listStyle: "none",
              padding: 0,
              margin: 0,
              maxHeight: "min(38dvh, 340px)",
              overflowY: "auto",
              scrollbarColor:
                "var(--bz-text-secondary) var(--bz-surface-overlay)",
            }}
            tabIndex={0}
          >
            {data.cues.map((cue, index) => (
              <li
                key={index}
                style={{
                  paddingBlock: 12,
                  borderBottom: "1px solid var(--bz-border-card)",
                }}
              >
                <Text
                  size="sm"
                  c="var(--bz-text-secondary)"
                  style={{ fontVariantNumeric: "tabular-nums" }}
                >
                  {timestamp(cue.start_ms)} to {timestamp(cue.end_ms)}
                </Text>
                <Text
                  mt={4}
                  style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
                >
                  {cue.text}
                </Text>
              </li>
            ))}
          </ol>
        </>
      )}
      {download?.row.id === row.id && download.status === "started" && (
        <Text role="status">Download started.</Text>
      )}
      {download?.row.id === row.id && download.status === "failed" && (
        <Text role="alert">
          Download failed. Retry or choose another subtitle.
        </Text>
      )}
      <Group justify="space-between" wrap="wrap" gap="sm">
        <Button
          variant="default"
          mih={44}
          aria-label="Close preview"
          onClick={close}
        >
          Close
        </Button>
        <Button
          mih={44}
          variant="light"
          disabled={status === "expired" || downloading}
          loading={downloading && download.row.id === row.id}
          onClick={() => void downloadSubtitle(row)}
        >
          Download SRT
        </Button>
      </Group>
    </Stack>
  );
}

export function DiscoverPreviewModal(
  props: Omit<PreviewProps, "preview"> & {
    preview: DiscoverPreviewFeedback | null;
  },
) {
  const reducedMotion = useReducedMotion();
  const opened = useRef(false);
  const source = useRef<string | null>(null);
  useEffect(() => {
    if (!props.preview) {
      if (opened.current) modals.close(MODAL_ID);
      opened.current = false;
      return;
    }
    source.current = previewSourceId(props.preview.row);
    const children = <SubtitlePreview {...props} preview={props.preview} />;
    if (opened.current) {
      modals.updateModal({ modalId: MODAL_ID, children });
      return;
    }
    opened.current = true;
    modals.open({
      modalId: MODAL_ID,
      title: "Subtitle preview",
      size: "lg",
      trapFocus: true,
      returnFocus: false,
      closeOnEscape: true,
      transitionProps: { duration: reducedMotion ? 0 : 200 },
      closeOnClickOutside: false,
      closeButtonProps: { "aria-label": "Close subtitle preview", size: 44 },
      styles: {
        content: {
          maxWidth: "calc(100vw - 24px)",
          background: "var(--bz-surface-overlay)",
          border: "1px solid var(--bz-border-card)",
          borderRadius: "var(--bz-radius-lg)",
        },
        header: { background: "transparent" },
        body: { background: "transparent" },
        overlay: { background: "rgba(0, 0, 0, 0.6)" },
        title: { fontWeight: 650 },
        close: { minWidth: 44, minHeight: 44 },
      },
      onClose: props.close,
      onExitTransitionEnd: () => {
        const button = source.current
          ? document.getElementById(source.current)
          : null;
        const opener =
          button instanceof HTMLButtonElement && button.disabled
            ? button.closest("article")
            : button;
        if (opener) {
          opener.scrollIntoView?.({ block: "nearest" });
          opener.focus({ preventScroll: true });
        }
      },
      children,
    });
  }, [props, reducedMotion]);
  useEffect(
    () => () => {
      modals.close(MODAL_ID);
    },
    [],
  );
  return null;
}
