import { FunctionComponent, useState } from "react";
import { Alert, Button, Divider, Select, Stack } from "@mantine/core";
import { useSubtitleArchiveDownload } from "@/apis/hooks";
import { useModals, withModal } from "@/modules/modals";

type Scope =
  | {
      kind: "series";
      seriesId: number;
      arrInstanceId?: number;
      seasons: number[];
    }
  | { kind: "movie"; radarrId: number; arrInstanceId?: number };

interface Props {
  scope: Scope;
  availableLanguages: string[];
}

const ALL = "all";

const SubtitleDownloadForm: FunctionComponent<Props> = ({
  scope,
  availableLanguages,
}) => {
  const [season, setSeason] = useState<string>(ALL);
  const [language, setLanguage] = useState<string>(ALL);
  const { mutateAsync, isPending } = useSubtitleArchiveDownload();
  const modals = useModals();

  const submit = async () => {
    const languageFilter = language === ALL ? undefined : language;
    try {
      if (scope.kind === "series") {
        await mutateAsync({
          kind: "series",
          seriesId: scope.seriesId,
          season: season === ALL ? undefined : Number(season),
          language: languageFilter,
          arrInstanceId: scope.arrInstanceId,
        });
      } else {
        await mutateAsync({
          kind: "movie",
          radarrId: scope.radarrId,
          language: languageFilter,
          arrInstanceId: scope.arrInstanceId,
        });
      }
      modals.closeSelf();
    } catch {
      // The hook already surfaced a notification; keep the modal open so the
      // user can adjust the selection.
    }
  };

  return (
    <Stack>
      <Alert>
        Downloads a zip of the subtitle files already on disk
        {scope.kind === "series" ? " for this series" : " for this movie"}.
        Embedded tracks are not included.
      </Alert>
      {scope.kind === "series" && (
        <Select
          label="Season"
          allowDeselect={false}
          value={season}
          onChange={(next) => setSeason(next ?? ALL)}
          data={[
            { value: ALL, label: "All seasons" },
            ...scope.seasons.map((s) => ({
              value: String(s),
              label: `Season ${String(s).padStart(2, "0")}`,
            })),
          ]}
        />
      )}
      <Select
        label="Language"
        allowDeselect={false}
        value={language}
        onChange={(next) => setLanguage(next ?? ALL)}
        data={[
          { value: ALL, label: "All languages" },
          ...availableLanguages.map((code) => ({
            value: code,
            label: code.toUpperCase(),
          })),
        ]}
      />
      <Divider />
      <Button loading={isPending} onClick={submit}>
        Download
      </Button>
    </Stack>
  );
};

export const SubtitleDownloadModal = withModal(
  SubtitleDownloadForm,
  "download-subtitles",
  {
    title: "Download subtitles",
  },
);

export default SubtitleDownloadForm;
