/* eslint-disable camelcase */
import { useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Divider,
  FileInput,
  Group,
  NumberInput,
  Select,
  Stack,
  TextInput,
} from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { ManualSearchView } from "@/components/modals/ManualSearchModal";
import { useModals, withModal } from "@/modules/modals";
import { searchAsJob, waitForJob } from "@/utilities/jobs";
import { useEnabledLanguages } from "@/utilities/languages";
import {
  sourceDetailApi,
  sourceDetailKeys,
  sourceError,
  SourceGroup,
  SourceItem,
  SourceItemTarget,
  useRefreshSourceDetails,
  useSourceWrite,
} from "./api";

function IdentityForm({
  item,
  kind,
}: {
  item: SourceGroup | SourceItem;
  kind: "groups" | "items";
}) {
  const modals = useModals();
  const write = useSourceWrite();
  const [title, setTitle] = useState(item.title);
  const [year, setYear] = useState<number | string>(
    item.year ? Number(item.year) : "",
  );
  const [season, setSeason] = useState<number | string>(
    "season" in item ? (item.season ?? "") : "",
  );
  const [episode, setEpisode] = useState<number | string>(
    "episode" in item ? (item.episode ?? "") : "",
  );
  const isEpisode = item.kind === "episode";
  const save = async (
    correction: Parameters<typeof sourceDetailApi.identify>[2],
  ) => {
    try {
      await write.mutateAsync(() =>
        sourceDetailApi.identify(
          kind,
          item.id,
          correction,
          item.kind === "show"
            ? "show"
            : item.kind === "episode"
              ? "episode"
              : "movie",
        ),
      );
      modals.closeSelf();
    } catch {
      /* The form shows the request error. */
    }
  };
  return (
    <Stack>
      {write.isError && <Alert color="red">{sourceError(write.error)}</Alert>}
      {(item.identity_candidates ?? []).map((candidate) => (
        <Button
          key={candidate.id}
          variant="subtle"
          disabled={write.isPending}
          onClick={() => void save({ candidate_id: candidate.id })}
        >
          {candidate.title}
          {candidate.year ? ` (${candidate.year})` : ""}
        </Button>
      ))}
      {(item.identity_candidates ?? []).length > 0 && <Divider />}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save({
            title: title.trim(),
            ...(typeof year === "number" ? { year } : {}),
            ...(isEpisode
              ? { season: Number(season), episode: Number(episode) }
              : {}),
          });
        }}
      >
        <Stack>
          <TextInput
            label="Title"
            value={title}
            onChange={(event) => setTitle(event.currentTarget.value)}
            required
            maxLength={500}
          />
          <NumberInput
            label="Year"
            value={year}
            onChange={setYear}
            min={1800}
            max={2200}
            allowDecimal={false}
            required={item.kind === "movie"}
          />
          {isEpisode && (
            <Group grow>
              <NumberInput
                label="Season"
                value={season}
                onChange={setSeason}
                min={0}
                allowDecimal={false}
                required
              />
              <NumberInput
                label="Episode"
                value={episode}
                onChange={setEpisode}
                min={0}
                allowDecimal={false}
                required
              />
            </Group>
          )}
          <Button
            type="submit"
            loading={write.isPending}
            disabled={
              !title.trim() || (isEpisode && (season === "" || episode === ""))
            }
          >
            Save
          </Button>
        </Stack>
      </form>
    </Stack>
  );
}

export const SourceIdentityModal = withModal(IdentityForm, "source-identity", {
  title: "Identity fix",
  size: "md",
});

function SourceSearchForm({
  item,
}: {
  item: SourceItemTarget & Pick<SourceItem, "scene_name">;
}) {
  const client = useQueryClient();
  const refresh = useRefreshSourceDetails();
  function useSearch() {
    const query = useQuery({
      queryKey: [
        ...sourceDetailKeys.root,
        "manual",
        item.kind,
        item.id,
        item.source_id,
        item.library_id,
        item.path,
      ],
      queryFn: async () => {
        await sourceDetailApi.requireCurrentItem(item);
        return searchAsJob<{
          data: SearchResultType[];
          providers?: System.ProviderSearchOutcome[];
        }>(client, () => sourceDetailApi.manualSearch(item.id));
      },
      enabled: false,
      retry: false,
      placeholderData: undefined,
    });
    return {
      ...query,
      data: query.data?.data,
      providers: query.data?.providers,
    };
  }
  return (
    <ManualSearchView
      item={{
        origin: "source",
        id: item.id,
        path: item.path,
        sceneName: item.scene_name ?? undefined,
      }}
      query={useSearch}
      preventRepeatDownload
      download={async (_, result) => {
        await sourceDetailApi.requireCurrentItem(item);
        const job = await sourceDetailApi.download(item.id, result);
        try {
          await waitForJob(client, job.job_id, job.session);
        } finally {
          await refresh();
        }
      }}
    />
  );
}

export const SourceManualSearchModal = withModal(
  SourceSearchForm,
  "source-manual-search",
  { title: "Search Subtitles", size: "calc(100vw - 4rem)" },
);

function SourceUploadForm({ item }: { item: SourceItemTarget }) {
  const { data: languages } = useEnabledLanguages();
  const client = useQueryClient();
  const modals = useModals();
  const write = useSourceWrite();
  const [file, setFile] = useState<File | null>(null);
  const [language, setLanguage] = useState<string | null>(null);
  const [hi, setHi] = useState(false);
  const [forced, setForced] = useState(false);
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!file || !language) return;
        void write
          .mutateAsync(async () => {
            await sourceDetailApi.requireCurrentItem(item);
            const job = await withInlineErrors(() =>
              sourceDetailApi.upload(item.id, file, language, hi, forced),
            );
            await waitForJob(client, job.job_id, job.session);
          })
          .then(() => modals.closeSelf())
          .catch(() => undefined);
      }}
    >
      <Stack>
        {write.isError && <Alert color="red">{sourceError(write.error)}</Alert>}
        <FileInput
          label="Subtitle"
          fileInputProps={{ "aria-label": "Subtitle file" }}
          required
          value={file}
          onChange={setFile}
        />
        <Select
          label="Language"
          required
          searchable
          value={language}
          onChange={setLanguage}
          data={languages.map((value) => ({
            value: value.code2,
            label: value.name,
          }))}
        />
        <Checkbox
          label="Hearing impaired"
          checked={hi}
          onChange={(event) => setHi(event.currentTarget.checked)}
        />
        <Checkbox
          label="Forced"
          checked={forced}
          onChange={(event) => setForced(event.currentTarget.checked)}
        />
        <Button
          type="submit"
          loading={write.isPending}
          disabled={!file || !language}
        >
          Upload
        </Button>
      </Stack>
    </form>
  );
}

export const SourceUploadModal = withModal(SourceUploadForm, "source-upload", {
  title: "Upload Subtitles",
  size: "md",
});
