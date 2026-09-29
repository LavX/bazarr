import { FunctionComponent } from "react";
import { Badge, Button, Group, Popover, Stack, Text } from "@mantine/core";
import { faEyeSlash } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useEmbeddedHiddenTracks,
  useEmbeddedTrackVisibility,
} from "@/apis/hooks";
import { HiddenTrackMediaType } from "@/apis/raw/subtitles";
import { toPython } from "@/utilities";
import {
  describeEmbeddedTrack,
  embeddedTrackLabel,
  hiddenTrackCodecs,
  isBitmapCodec,
  subtitleCodecLabel,
} from "@/utilities/subtitles";

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
  /**
   * "start", the default, for a disclosure that follows a row's badges.
   * "end" for one on its own line under a table: the trigger sits at the end
   * of that line and the list opens from there.
   */
  align?: "start" | "end";
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
  align = "start",
}) => {
  const { data } = useEmbeddedHiddenTracks(mediaType, batchIds ?? [mediaId]);
  const { unhide } = useEmbeddedTrackVisibility();
  const tracks = data?.get(mediaId);

  if (!tracks || tracks.length === 0) {
    return null;
  }

  const disclosure = (
    <Popover
      position={align === "end" ? "bottom-end" : "bottom-start"}
      // The arrow follows the trigger even after the list is shifted to fit
      // a narrow screen, and the list keeps the page's 16px gutter.
      withArrow
      arrowPosition="center"
      middlewares={{ flip: true, shift: { padding: 16 } }}
      shadow="md"
      withinPortal
    >
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
      <Popover.Dropdown maw="calc(100vw - 32px)">
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
                gap="sm"
              >
                <Group gap={6} wrap="wrap">
                  <Text size="sm">{label}</Text>
                  {/* The badge the movie row shows for the same track. */}
                  {hiddenTrackCodecs(track.codec).map((codec) => (
                    <Badge
                      key={codec}
                      size="xs"
                      variant="light"
                      color={isBitmapCodec(codec) ? "gray" : undefined}
                      title={describeEmbeddedTrack("Subtitle format", codec)}
                    >
                      {subtitleCodecLabel(codec)}
                    </Badge>
                  ))}
                </Group>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="gray"
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

  // Under a table the parent stacks its children and would stretch the
  // trigger across the page, leaving the list to open from the far edge. The
  // padding matches the table cells', so the trigger lines up with the rows.
  return align === "end" ? (
    <Group justify="flex-end" px="sm">
      {disclosure}
    </Group>
  ) : (
    disclosure
  );
};

export default HiddenTracks;
