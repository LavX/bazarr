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
import { groupSeerrSeasons } from "./seerrSeasons";
import styles from "./Discover.module.scss";

type Props = {
  title: MetadataTitle;
  state: SeerrMediaState;
  tmdbId: number;
  libraryUncertain?: boolean;
  onClose: () => void;
  onSubmit: (body: SeerrRequestBody) => void;
};

export default function SeerrRequestModal({
  title,
  state,
  tmdbId,
  libraryUncertain = false,
  onClose,
  onSubmit,
}: Props) {
  const isShow = title.media_type === "show";
  const groups = useMemo(() => groupSeerrSeasons(title, state), [title, state]);
  // The only lane open is the 4K one: there is no valid non-4K request to
  // fall back to, so is4k starts (and, for a show, stays) forced true rather
  // than defaulting to the usual unchecked box. A show's `requestable` only
  // reports "Seerr has not blocklisted it", and stays true once every season
  // is taken, so for a show the season arithmetic decides this instead.
  const fourKOnly =
    (!state.requestable || groups.exhausted) && state.requestable_4k;
  const [is4k, set4k] = useState(fourKOnly);
  const [chosen, setChosen] = useState<number[]>([]);
  const tvdbId =
    "tvdb_id" in title && typeof title.tvdb_id === "number"
      ? title.tvdb_id
      : undefined;

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

  // Nothing to choose when the 4K lane is the only one open: is4k is fixed at
  // true, so a checkbox that could uncheck it would offer a choice that
  // cannot actually be submitted.
  const fourK = state.requestable_4k && !fourKOnly && (
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
          {fourKOnly && (
            <Text size="sm">Only the 4K version is available to request.</Text>
          )}
          {fourK}
          <Text size="xs" c="dimmed">
            Requested as the Seerr owner and approved immediately.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={() => onSubmit(body(undefined))}>
              {fourKOnly ? "Request the 4K version" : "Request in Seerr"}
            </Button>
          </Group>
        </Stack>
      </Modal>
    );
  }

  // A 4K show request has no per-season 4K state to draw from: Seerr reports
  // season status for the non-4K lane only. So any 4K show request collapses
  // to the same whole-series fallback as "Seerr forbids partial requests" or
  // "the season list is unavailable". Sending the groups shown here alongside
  // is4k would ask for seasons Seerr may already hold at 4K, which it answers
  // with a 409.
  const allOnly =
    fourKOnly || is4k || !state.partial_requests || !groups.hasList;
  const allOnlyReason = fourKOnly
    ? "Only the 4K version is available to request."
    : is4k
      ? "Seerr does not report 4K availability season by season, so the whole series is requested."
      : !state.partial_requests
        ? "Seerr only accepts whole-series requests."
        : "Season details are unavailable.";
  const label = (s: { season: number; title: string }) =>
    s.title || `Season ${s.season}`;

  return (
    <Modal opened onClose={onClose} title={`Request ${title.title}`}>
      <Stack>
        {libraryUncertain && (
          <Text size="sm" c="dimmed">
            Your library check is incomplete, so what you already own may be
            missing from these groups.
          </Text>
        )}
        {allOnly ? (
          <Text size="sm">{allOnlyReason}</Text>
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
              <fieldset className={styles.seerrGroup}>
                <legend>
                  <Text span fw={600} size="sm">
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
            <fieldset className={styles.seerrGroup}>
              <legend>
                <Text span fw={600} size="sm">
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
