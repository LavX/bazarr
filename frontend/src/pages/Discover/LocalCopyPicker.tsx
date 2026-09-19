import { useState } from "react";
import { Link, useLocation } from "react-router";
import { Alert, Anchor, Button, Group, Text } from "@mantine/core";
import { useDiscoverCopies } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { DiscoverCopy, DiscoverCopyOption } from "@/types/discover";
import DiscoverSelect from "./DiscoverSelect";
import styles from "./Discover.module.scss";

// Title-only is a real choice with a real label, so it needs a real value: an
// empty string is what a select renders as nothing chosen, which left the
// default state showing a blank field instead of the option it is on. The
// sentinel cannot collide with a copy id, which always starts "c1.".
const TITLE_ONLY = "title-only";

export function copyLibraryRoute(copy: DiscoverCopy): string | null {
  if (copy.media_type === "movie") return `/movies/${copy.local_id}`;
  return copy.series_local_id === null
    ? null
    : `/series/${copy.series_local_id}`;
}

export function copyOwner(copy: DiscoverCopy): string {
  if (copy.arr_instance_id === null) return "Owner unknown";
  return copy.instance_name ?? `Instance ${copy.arr_instance_id}`;
}

export function copyRelease(copy: DiscoverCopy): string {
  return copy.release ?? copy.filename ?? "Release information unavailable";
}

export function copyFacts(copy: DiscoverCopy): string {
  return [copy.resolution, copy.source, copy.video_codec, copy.audio_codec]
    .filter(Boolean)
    .join(" · ");
}

/** The one line a copy is offered under, in the control and in its tests. */
export function describeCopy(copy: DiscoverCopy): string {
  return [
    copyOwner(copy),
    `Local ${copy.media_type} ${copy.local_id}`,
    copyRelease(copy),
    copyFacts(copy),
  ]
    .filter(Boolean)
    .join(" · ");
}

function fileSize(bytes: number): string {
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  return `${bytes} bytes`;
}

// The three reasons the server can report today, plus an honest fallback. A
// reason it does not recognise must not be relabelled as one it does.
const unavailableReasons: [string, string][] = [
  ["owner_unknown", "Its owning instance is unknown"],
  ["no_stored_path", "No file is recorded for this item"],
  ["instance_missing", "Its instance is no longer configured"],
];

function unavailable(copy: DiscoverCopyOption): string {
  const known = unavailableReasons.find(
    ([reason]) => reason === copy.unavailable_reason,
  );
  return known ? known[1] : "It cannot be used for this search";
}

/**
 * Explicit matching against one copy in the reader's own library.
 *
 * Nothing here is automatic. A confirmed target with exactly one copy still
 * defaults to title-only matching, because adopting a file changes what is
 * sent to providers and only the reader knows whether that file is the one
 * they mean. The chosen copy is search context: it supplies the file name,
 * size, release description and content hash providers see, and it certifies
 * nothing about synchronization.
 */
export default function LocalCopyPicker() {
  const { state, updateDraft, updateBrowsing } = useDiscover();
  const { draft } = state;
  const location = useLocation();
  const imdbId = draft.imdbId.trim().toLowerCase();
  const episode = draft.mediaType === "episode";
  const target =
    draft.mode === "release" || !/^tt\d{7,10}$/.test(imdbId)
      ? null
      : !episode
        ? { mediaType: "movie" as const, imdbId }
        : /^\d{1,4}$/.test(draft.season) && /^[1-9]\d{0,3}$/.test(draft.episode)
          ? {
              mediaType: "episode" as const,
              imdbId,
              season: Number(draft.season),
              episode: Number(draft.episode),
            }
          : null;
  const offer = useDiscoverCopies(target);
  const items = offer.data?.items ?? [];
  const chosen = items.find((item) => item.copy_id === draft.copyId);
  // A selection the list does not contain. The synthetic option below is keyed
  // on this alone, so the control can never show a value it has no option for.
  const unmatched = draft.copyId !== undefined && !chosen;
  // Why it is missing has exactly two answers once a list exists, and they are
  // not interchangeable. A complete list that lacks it is evidence the copy is
  // gone. A truncated list that lacks it is evidence of nothing: the copy may
  // sit past COPY_OPTION_LIMIT and still resolve server side.
  //
  // `offer.data` is the last payload a successful read produced, so a first
  // load that failed has none and the page claims neither. A later refetch
  // failing does not unlearn what the successful one showed, which is why both
  // are keyed on the data rather than on the current status.
  const retired =
    unmatched && offer.data !== undefined && !offer.data.truncated;
  const unlisted =
    unmatched && offer.data !== undefined && offer.data.truncated;
  const route = chosen ? copyLibraryRoute(chosen) : null;
  // Optional by design, and closed until the reader wants it. A chosen copy
  // holds it open, because its state (the file, a retired choice, a copy past
  // the list) must stay in view while it is in use.
  const [opened, setOpened] = useState(false);
  const open = opened || draft.copyId !== undefined;
  if (target === null || (!items.length && draft.copyId === undefined))
    return null;
  const kind = episode ? "episode" : "film";

  return (
    <details
      className={styles.copyDisclosure}
      open={open}
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary>
        <span>Match a copy in your library</span>
      </summary>
      <div className={styles.copyBody}>
        <Text size="sm">
          Optional. A chosen copy sends that file&apos;s name, size, release
          details and content hash to providers as search context. It does not
          verify subtitle timing, and nothing is saved to your library.
        </Text>
        {offer.isPending && <Text role="status">Loading library copies.</Text>}
        {offer.isError && (
          <Alert color="yellow">
            Your library copies could not be read
            {offer.data === undefined
              ? ""
              : ", so the list below may be out of date"}
            .{" "}
            {draft.copyId === undefined
              ? "Title-only matching is unaffected."
              : "Your chosen copy is still in use for the next search."}
            <Group gap="xs" mt="xs">
              <Button variant="subtle" onClick={() => void offer.refetch()}>
                Retry library copies
              </Button>
              {draft.copyId !== undefined && (
                <Button
                  variant="subtle"
                  onClick={() => updateDraft({ copyId: undefined })}
                >
                  Search the title only
                </Button>
              )}
            </Group>
          </Alert>
        )}
        {/* A failed refetch keeps the last good list, so the reader keeps the
          control and the choice. Only a first load with nothing to show at all
          leaves the alert on its own. */}
        {offer.data !== undefined && (
          <DiscoverSelect
            id="discover-local-copy"
            label="Library copy"
            description={`Title only is the default. Choosing a copy does not change which ${kind} is searched.`}
            value={draft.copyId ?? TITLE_ONLY}
            options={[
              { value: TITLE_ONLY, label: "Title only, no library copy" },
              ...items.map((item) => ({
                value: item.copy_id,
                label: item.selectable
                  ? describeCopy(item)
                  : `${describeCopy(item)} · Cannot be used: ${unavailable(item)}`,
                disabled: !item.selectable,
              })),
              ...(unmatched
                ? [
                    {
                      value: draft.copyId as string,
                      label: retired
                        ? "Previously chosen copy, no longer offered"
                        : "Previously chosen copy, beyond this list",
                    },
                  ]
                : []),
            ]}
            onChange={(value) => {
              updateDraft({ copyId: value === TITLE_ONLY ? undefined : value });
              // The position is deliberately not written here. This control sits
              // inside the Discover section, so the page's own focusin, scroll
              // and capture-phase click listeners already record it into the
              // right slot for whichever branch is rendering.
            }}
          />
        )}
        {retired && (
          <Alert color="yellow">
            The copy you chose is no longer offered for this {kind}. Choose
            another copy, or return to title only. Until you do, searching sends
            that copy and the server answers that it cannot be used.
          </Alert>
        )}
        {unlisted && (
          <Alert color="yellow">
            Your library holds more copies of this {kind} than are listed here,
            and the one you chose is not among those shown. It may still be
            usable. Searching sends it, and the server decides.
          </Alert>
        )}
        {chosen && (
          <>
            {/* What the option label cannot show: the file behind the release. */}
            <Text size="sm">
              Chosen file: {chosen.filename ?? "File name unavailable"}
              {chosen.file_size === null
                ? ""
                : ` · ${fileSize(chosen.file_size)}`}
              {chosen.updated_at === null
                ? ""
                : ` · Library record updated ${new Date(chosen.updated_at).toLocaleDateString()}`}
            </Text>
            {route && (
              <Anchor
                c="var(--discover-link)"
                component={Link}
                id="discover-copy-library-item"
                to={route}
                py="sm"
                onClick={() =>
                  updateBrowsing({
                    returnTarget:
                      location.pathname + location.search + location.hash,
                  })
                }
              >
                Open this item in your library
              </Anchor>
            )}
          </>
        )}
        {offer.isSuccess && !items.length && !offer.isFetching && (
          <Text size="sm">
            {episode && offer.data.owning_titles > 0
              ? `${offer.data.owning_titles} series in your library match this title, but none holds this episode.`
              : `No copy of this ${kind} is in your library.`}
          </Text>
        )}
        {offer.data?.truncated && !unlisted && (
          <Text size="sm">
            More copies of this {kind} exist than are listed here.
          </Text>
        )}
      </div>
    </details>
  );
}
