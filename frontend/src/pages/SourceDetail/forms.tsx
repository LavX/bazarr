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
import {
  expireSourceSubtitleDocument,
  sourceSubtitleVariant,
} from "@/apis/queries/sourceSubtitleDocument";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { SubtitleBatchUploadForm } from "@/components/forms/MovieUploadForm";
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
  const originalSeason = "season" in item ? item.season : null;
  const originalEpisode = "episode" in item ? item.episode : null;
  const originalDate = "event_date" in item ? item.event_date : null;
  const [season, setSeason] = useState<number | string>(originalSeason ?? "");
  const [episode, setEpisode] = useState<number | string>(
    originalEpisode ?? "",
  );
  const isEpisode = item.kind === "episode";
  const isSport = item.kind === "sports_event";
  const [date, setDate] = useState(originalDate ?? "");
  const [part, setPart] = useState(
    "part_name" in item ? (item.part_name ?? "") : "",
  );
  const save = async (
    correction: Parameters<typeof sourceDetailApi.identify>[2],
  ) => {
    try {
      await write.mutateAsync(() =>
        sourceDetailApi.identify(kind, item.id, correction, item.kind),
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
            ...(isSport
              ? {
                  ...(date || originalDate != null
                    ? { event_date: date || null }
                    : {}),
                  ...(typeof season === "number" || originalSeason != null
                    ? { season: typeof season === "number" ? season : null }
                    : {}),
                  ...(typeof episode === "number" || originalEpisode != null
                    ? { round: typeof episode === "number" ? episode : null }
                    : {}),
                  part_name: part.trim() || null,
                }
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
          {isSport && (
            <Group grow>
              <TextInput
                type="date"
                label="Date"
                value={date}
                onChange={(event) => setDate(event.currentTarget.value)}
              />
              <TextInput
                label="Part"
                value={part}
                onChange={(event) => setPart(event.currentTarget.value)}
              />
            </Group>
          )}
          {(isEpisode || isSport) && (
            <Group grow>
              <NumberInput
                label="Season"
                value={season}
                onChange={setSeason}
                min={0}
                allowDecimal={false}
                required={isEpisode}
              />
              <NumberInput
                label={isSport ? "Round" : "Episode"}
                value={episode}
                onChange={setEpisode}
                min={0}
                allowDecimal={false}
                required={isEpisode}
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
        item.group_id,
        item.path,
      ],
      queryFn: async () => {
        await sourceDetailApi.requireCurrentManualItem(item);
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
        await sourceDetailApi.requireCurrentManualItem(item);
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

function useSourceSubtitleUpload(item: SourceItemTarget) {
  const client = useQueryClient();
  const write = useSourceWrite();
  const upload = ({ file, language, hi, forced }: FormType.UploadSubtitle) =>
    write.mutateAsync(async () => {
      await sourceDetailApi.requireCurrentManualItem(item);
      const job = await withInlineErrors(() =>
        sourceDetailApi.upload(item.id, file, language, hi, forced),
      );
      try {
        await waitForJob(client, job.job_id, job.session);
      } finally {
        void expireSourceSubtitleDocument(
          client,
          item.id,
          sourceSubtitleVariant(language, hi, forced),
        );
      }
    });
  return { upload, write };
}

function SourceUploadForm({ item }: { item: SourceItemTarget }) {
  const { data: languages } = useEnabledLanguages();
  const modals = useModals();
  const { upload, write } = useSourceSubtitleUpload(item);
  const [file, setFile] = useState<File | null>(null);
  const [language, setLanguage] = useState<string | null>(null);
  const [hi, setHi] = useState(false);
  const [forced, setForced] = useState(false);
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!file || !language) return;
        void upload({ file, language, hi, forced })
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

function SourceDropUploadForm({
  item,
  files,
}: {
  item: SourceItem;
  files: File[];
}) {
  const { data: languages } = useEnabledLanguages();
  const { upload, write } = useSourceSubtitleUpload(item);
  return (
    <SubtitleBatchUploadForm
      files={files}
      subtitles={item.subtitles}
      languages={languages}
      defaultLanguage={null}
      capturePageDrops
      independentFlags
      upload={upload}
      error={
        write.isError && <Alert color="red">{sourceError(write.error)}</Alert>
      }
    />
  );
}

export const SourceDropUploadModal = withModal(
  SourceDropUploadForm,
  "source-drop-upload",
  { title: "Upload Subtitles", size: "xl" },
);
