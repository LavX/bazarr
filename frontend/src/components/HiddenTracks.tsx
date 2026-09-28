import { FunctionComponent } from "react";
import { Button, Group, Popover, Stack, Text } from "@mantine/core";
import { faEyeSlash } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useEmbeddedHiddenTracks,
  useEmbeddedTrackVisibility,
} from "@/apis/hooks";
import { HiddenTrackMediaType } from "@/apis/raw/subtitles";
import { toPython } from "@/utilities";
import { embeddedTrackLabel } from "@/utilities/subtitles";

interface Props {
  mediaType: HiddenTrackMediaType;
  /** Local id of the movie, episode or event this disclosure belongs to. */
  mediaId: number;
  /**
   * Every id on the page. Rows passing the same list share one request, and
   * the query lives here rather than in the table so its arrival re-renders
   * only these disclosures, not the rows whose menus may be open.
   */
  batchIds?: number[];
  disabled?: boolean;
}

/**
 * "Hidden tracks (n)": the embedded tracks the user hid on one title, each
 * with Unhide. Renders nothing when there are none, so it costs a page
 * without hidden tracks no space at all.
 */
const HiddenTracks: FunctionComponent<Props> = ({
  mediaType,
  mediaId,
  batchIds,
  disabled,
}) => {
  const { data } = useEmbeddedHiddenTracks(mediaType, batchIds ?? [mediaId]);
  const { unhide } = useEmbeddedTrackVisibility();
  const tracks = data?.get(mediaId);

  if (!tracks || tracks.length === 0) {
    return null;
  }

  return (
    <Popover position="bottom-start" withArrow shadow="md" withinPortal>
      <Popover.Target>
        <Button
          variant="subtle"
          color="gray"
          size="compact-xs"
          leftSection={<FontAwesomeIcon icon={faEyeSlash} />}
        >
          Hidden tracks ({tracks.length})
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs" maw={360}>
          <Text size="xs" c="dimmed">
            Bazarr+ does not list these embedded tracks for this title. They are
            still in the media file.
          </Text>
          {tracks.map((track) => {
            const label = embeddedTrackLabel(track);
            return (
              <Group
                key={`${track.media_id}:${track.language}`}
                justify="space-between"
                wrap="nowrap"
              >
                <Text size="sm">
                  {label}
                  {track.codec ? (
                    <Text span size="xs" c="dimmed">
                      {` ${track.codec}`}
                    </Text>
                  ) : null}
                </Text>
                <Button
                  size="compact-xs"
                  variant="light"
                  aria-label={`Unhide ${label}`}
                  disabled={disabled || unhide.isPending}
                  onClick={() =>
                    unhide.mutate({
                      /* eslint-disable camelcase */
                      media_type: track.media_type,
                      media_id: track.media_id,
                      arr_instance_id: track.arr_instance_id,
                      /* eslint-enable camelcase */
                      language: track.code2,
                      hi: toPython(track.hi),
                      forced: toPython(track.forced),
                    })
                  }
                >
                  Unhide
                </Button>
              </Group>
            );
          })}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
};

export default HiddenTracks;
