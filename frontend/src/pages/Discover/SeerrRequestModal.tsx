/* eslint-disable camelcase -- transport field names. */
import { useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  Group,
  List,
  Modal,
  Stack,
  Text,
} from "@mantine/core";
import type { MetadataTitle } from "@/types/discover";
import type { SeerrMediaState, SeerrRequestBody } from "@/types/seerr";

type Props = {
  title: MetadataTitle;
  state: SeerrMediaState;
  tmdbId: number;
  onClose: () => void;
  onSubmit: (body: SeerrRequestBody) => void;
};

export default function SeerrRequestModal({
  title,
  state,
  tmdbId,
  onClose,
  onSubmit,
}: Props) {
  const isShow = title.media_type === "show";
  const [is4k, set4k] = useState(false);
  const [chosen, setChosen] = useState<number[]>([]);
  const tvdbId =
    "tvdb_id" in title && typeof title.tvdb_id === "number"
      ? title.tvdb_id
      : undefined;

  const groups = useMemo(() => {
    const seasons = (
      "seasons" in title && Array.isArray(title.seasons) ? title.seasons : []
    ).filter((s) => state.special_episodes || s.season > 0);
    const seerr = new Map(state.seasons.map((s) => [s.number, s.state]));
    const owned = new Set(
      ("ownership" in title && title.ownership?.seasons_owned) || [],
    );
    const inSeerr = seasons.filter(
      (s) =>
        seerr.get(s.season) === "requested" ||
        seerr.get(s.season) === "available",
    );
    const rest = seasons.filter((s) => !inSeerr.includes(s));
    return {
      inSeerr,
      owned: rest.filter((s) => owned.has(s.season)),
      open: rest.filter((s) => !owned.has(s.season)),
      hasList: seasons.length > 0,
    };
  }, [title, state]);

  const toggle = (season: number) =>
    setChosen((current) =>
      current.includes(season)
        ? current.filter((s) => s !== season)
        : [...current, season],
    );

  const body = (seasons: number[] | "all" | undefined): SeerrRequestBody => ({
    media_type: isShow ? "tv" : "movie",
    tmdb_id: tmdbId,
    ...(tvdbId ? { tvdb_id: tvdbId } : {}),
    ...(seasons ? { seasons } : {}),
    is4k,
  });

  const fourK = state.requestable_4k && (
    <Checkbox
      label="Request the 4K version"
      checked={is4k}
      onChange={(e) => set4k(e.currentTarget.checked)}
    />
  );

  if (!isShow) {
    return (
      <Modal opened onClose={onClose} title={`Request ${title.title}`}>
        <Stack>
          {fourK}
          <Text size="xs" c="dimmed">
            Requested as the Seerr owner and approved immediately.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={() => onSubmit(body(undefined))}>
              Request in Seerr
            </Button>
          </Group>
        </Stack>
      </Modal>
    );
  }

  const allOnly = !state.partial_requests || !groups.hasList;
  const label = (s: { season: number; title: string }) =>
    s.title || `Season ${s.season}`;

  return (
    <Modal opened onClose={onClose} title={`Request ${title.title}`}>
      <Stack>
        {allOnly ? (
          <Text size="sm">
            {state.partial_requests
              ? "Season details are unavailable."
              : "Seerr only accepts whole-series requests."}
          </Text>
        ) : (
          <>
            {groups.inSeerr.length > 0 && (
              <div>
                <Text fw={600} size="sm">
                  Already in Seerr
                </Text>
                <List size="sm">
                  {groups.inSeerr.map((s) => (
                    <List.Item key={s.season}>{label(s)}</List.Item>
                  ))}
                </List>
              </div>
            )}
            {groups.owned.length > 0 && (
              <fieldset className="seerr-group">
                <legend>
                  <Text fw={600} size="sm">
                    In your Bazarr+ library
                  </Text>
                </legend>
                {groups.owned.map((s) => (
                  <Checkbox
                    key={s.season}
                    label={label(s)}
                    checked={chosen.includes(s.season)}
                    onChange={() => toggle(s.season)}
                  />
                ))}
              </fieldset>
            )}
            <fieldset className="seerr-group">
              <legend>
                <Text fw={600} size="sm">
                  Available to request
                </Text>
              </legend>
              {groups.open.length === 0 && (
                <Text size="sm" c="dimmed">
                  Nothing left to request.
                </Text>
              )}
              {groups.open.map((s) => (
                <Checkbox
                  key={s.season}
                  label={label(s)}
                  checked={chosen.includes(s.season)}
                  onChange={() => toggle(s.season)}
                />
              ))}
            </fieldset>
          </>
        )}
        {fourK}
        <Text size="xs" c="dimmed">
          Requested as the Seerr owner and approved immediately.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          {allOnly ? (
            <Button onClick={() => onSubmit(body("all"))}>
              Request all seasons
            </Button>
          ) : (
            <Button
              disabled={chosen.length === 0}
              onClick={() => onSubmit(body([...chosen].sort((a, b) => a - b)))}
            >
              {`Request ${chosen.length} season${chosen.length === 1 ? "" : "s"}`}
            </Button>
          )}
        </Group>
      </Stack>
    </Modal>
  );
}
