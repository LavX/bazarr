import { useState } from "react";
import {
  Alert,
  Checkbox,
  Group,
  NativeSelect,
  Stack,
  Text,
} from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import sports, { SportsEventReference } from "@/apis/raw/sports";
import { withModal } from "@/modules/modals";
import { waitForJob } from "@/utilities/jobs";
import {
  useEnabledLanguages,
  useLanguageProfileBy,
} from "@/utilities/languages";
import { ManualSearchView } from "./ManualSearchModal";

// What the modal actually needs: the ids the sports routes are keyed on, plus
// the profile it offers languages from. Typed to that rather than to a full
// SportsEvent so a caller holding a row rather than an event, the wanted page,
// can open it without inventing the fields it never reads.
export type SportsSearchTarget = Pick<
  SportsEventReference,
  "id" | "arr_instance_id"
> & {
  profileId: number | null;
  /** Shown as the resource hint when the caller has the recording path. */
  path?: string;
  /** Shown as the release hint when the indexer recorded one. */
  sceneName?: string;
};

function SportsSearchView({
  item,
  language: initialLanguage,
  hi: initialHi = false,
  forced: initialForced = false,
}: {
  item: SportsSearchTarget;
  // Opened from a missing-language badge, the search starts on that language
  // and its modifiers rather than on the profile's first entry, which is
  // rarely the one the user just clicked.
  language?: string;
  hi?: boolean;
  forced?: boolean;
}) {
  const client = useQueryClient();
  const { data: languages } = useEnabledLanguages();
  const profile = useLanguageProfileBy(item.profileId);
  const [selected, setSelected] = useState<string | undefined>(initialLanguage);
  const language =
    selected ?? profile?.items[0]?.language ?? languages[0]?.code2 ?? "";
  const [hi, setHi] = useState(initialHi);
  const [forced, setForced] = useState(initialForced);
  const [downloading, setDownloading] = useState(false);
  const [queued, setQueued] = useState(false);
  // A job can complete with a warning the queue records as its last progress
  // message (published, but part of the follow-up work did not finish). The
  // job resolves rather than rejects, so the message needs its own channel to
  // the user, which this is.
  const [jobWarning, setJobWarning] = useState<string | null>(null);

  function useSearch() {
    return useQuery({
      queryKey: [
        QueryKeys.Sports,
        "search",
        item.arr_instance_id,
        item.id,
        language,
        hi,
        forced,
      ],
      queryFn: () => sports.searchSubtitles(item, language, hi, forced),
      enabled: false,
      retry: false,
    });
  }
  async function download(
    event: SportsSearchTarget,
    candidate: SearchResultType,
  ) {
    if (downloading) return;
    setDownloading(true);
    setJobWarning(null);
    try {
      // Queued as a backend job, the way the library's manual download is.
      // The row is marked only once the job completes: a failed or stopped
      // job rejects with its reason and leaves the row retryable.
      const { job_id: jobId } = await sports.downloadSubtitle(event, candidate);
      setQueued(jobId != null);
      const job = await waitForJob(client, jobId);
      if (job?.progress_message) {
        setJobWarning(job.progress_message);
      }
    } finally {
      setQueued(false);
      // The sports root, not just "events". Wanted, history and blacklist are
      // all cached under [Sports, <kind>, ...], so invalidating only "events"
      // left a downloaded language still showing as missing on the Wanted
      // page until a manual reload. Badges carries the sidebar count.
      await client.invalidateQueries({ queryKey: [QueryKeys.Sports] });
      await client.invalidateQueries({ queryKey: [QueryKeys.Badges] });
      setDownloading(false);
    }
  }
  return (
    <Stack>
      <Group>
        <NativeSelect
          label="Language"
          value={language}
          onChange={(event) => setSelected(event.currentTarget.value)}
          data={languages.map((value) => ({
            value: value.code2,
            label: value.name,
          }))}
          disabled={downloading}
        />
        <Checkbox
          label="Hearing impaired"
          checked={hi}
          onChange={(event) => setHi(event.currentTarget.checked)}
          disabled={downloading}
        />
        <Checkbox
          label="Forced"
          checked={forced}
          onChange={(event) => setForced(event.currentTarget.checked)}
          disabled={downloading}
        />
      </Group>
      {queued && (
        <Text size="sm" c="dimmed" role="status">
          Queued in Jobs. You can close this window; Jobs reports the result.
        </Text>
      )}
      {jobWarning && <Alert color="yellow">{jobWarning}</Alert>}
      <ManualSearchView
        key={`${language}:${hi}:${forced}`}
        item={item}
        query={useSearch}
        download={download}
        preventRepeatDownload
        searchDisabled={!language || downloading}
      />
    </Stack>
  );
}

export const SportsSearchModal = withModal(
  (props: Parameters<typeof SportsSearchView>[0]) => (
    <SportsSearchView
      key={`${props.item.arr_instance_id}:${props.item.id}:${props.language}:${props.hi}:${props.forced}`}
      {...props}
    />
  ),
  "sports-manual-search",
  {
    title: "Search Subtitles",
    size: "calc(100vw - 4rem)",
  },
);
