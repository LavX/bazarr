import React, {
  FunctionComponent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  Button,
  Divider,
  MantineColor,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { Dropzone } from "@mantine/dropzone";
import { useForm } from "@mantine/form";
import { showNotification } from "@mantine/notifications";
import {
  faCheck,
  faCircleNotch,
  faInfoCircle,
  faTimes,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { uniqBy } from "lodash";
import {
  useSportsEvents,
  useSportsSubtitleModification,
} from "@/apis/hooks/sports";
import api from "@/apis/raw";
import type { SportsEvent, SportsLeague } from "@/apis/raw/sports";
import { subtitlesTypeOptions } from "@/components/forms/uploadFormSelectorTypes";
import { eventBelongsToLeague } from "@/components/forms/uploadHelpers";
import { Action, DropContent, Selector } from "@/components/inputs";
import SimpleTable from "@/components/tables/SimpleTable";
import TextPopover from "@/components/TextPopover";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/task";
import { useArrayAction, useSelectorOptions } from "@/utilities";
import { expandArchives, isArchiveFile } from "@/utilities/archives";
import FormUtils from "@/utilities/form";
import {
  useLanguageProfileBy,
  useProfileItemsToLanguages,
} from "@/utilities/languages";

type SubtitleFile = {
  file: File;
  language: Language.Info | null;
  forced: boolean;
  hi: boolean;
  event: SportsEvent | null;
  validateResult?: SubtitleValidateResult;
};

type SubtitleValidateResult = {
  state: "valid" | "warning" | "error";
  messages?: string;
};

const validator = (
  file: SubtitleFile,
  leagueId: number,
): SubtitleValidateResult => {
  if (file.language === null) {
    return { state: "error", messages: "Language is not selected" };
  }
  if (file.event === null) {
    return { state: "error", messages: "Event is not selected" };
  }
  if (!eventBelongsToLeague(file.event, leagueId)) {
    return {
      state: "error",
      messages: "Event does not belong to this league",
    };
  }
  // An event stores its subtitles as [language key, path, size] tuples, so the
  // key is compared on its base language: uploading en over an existing en:hi
  // is still a different file.
  const existing = (file.event.subtitles ?? []).find(
    ([key, path]) => key === languageKey(file) && Boolean(path),
  );
  if (existing !== undefined) {
    return { state: "warning", messages: "Override existing subtitle" };
  }
  return { state: "valid" };
};

function languageKey(file: SubtitleFile): string {
  const code = file.language?.code2 ?? "";
  if (file.hi) return `${code}:hi`;
  if (file.forced) return `${code}:forced`;
  return code;
}

interface Props {
  files: File[];
  league: SportsLeague;
  onComplete?: VoidFunction;
}

// The sports counterpart of SeriesUploadForm. A league stands where a show
// does and an event where an episode does; the differences are that a sports
// event carries its subtitles as tuples rather than Subtitle objects, and that
// there is no filename-to-event matcher, because a sports filename encodes a
// date and a fixture rather than a season/episode pair the info endpoint can
// resolve.
const SportsUploadForm: FunctionComponent<Props> = ({
  league,
  files,
  onComplete,
}) => {
  const modals = useModals();
  const eventsQuery = useSportsEvents(league.id, league.arr_instance_id, 1);
  const eventList = useMemo(
    () => eventsQuery.data?.data ?? [],
    [eventsQuery.data],
  );
  const eventOptions = useSelectorOptions(
    eventList,
    (v) => (v.partName ? `${v.title} (${v.partName})` : v.title),
    (v) => v.id.toString(),
  );

  const profile = useLanguageProfileBy(league.profileId);
  const languages = useProfileItemsToLanguages(profile);
  const languageOptions = useSelectorOptions(
    uniqBy(languages, "code2"),
    (v) => v.name,
    (v) => v.code2,
  );

  const defaultLanguage = useMemo(
    () => (languages.length > 0 ? languages[0] : null),
    [languages],
  );

  // One event in the league means there is nothing to choose, so it is chosen.
  const defaultEvent = useMemo(
    () => (eventList.length === 1 ? eventList[0] : null),
    [eventList],
  );

  const buildRow = useCallback(
    (file: File): SubtitleFile => {
      const row: SubtitleFile = {
        file,
        language: defaultLanguage,
        forced: defaultLanguage?.forced ?? false,
        hi: defaultLanguage?.hi ?? false,
        event: defaultEvent,
      };
      return { ...row, validateResult: validator(row, league.id) };
    },
    [defaultLanguage, defaultEvent, league.id],
  );

  const [processing, setProcessing] = useState(false);

  const form = useForm({
    initialValues: {
      files: files.filter((file) => !isArchiveFile(file)).map(buildRow),
    },
    validate: {
      files: FormUtils.validation(
        (values: SubtitleFile[]) =>
          values.find(
            (v: SubtitleFile) =>
              v.language === null ||
              v.event === null ||
              v.validateResult === undefined ||
              v.validateResult.state === "error",
          ) === undefined,
        "Some files cannot be uploaded, please check",
      ),
    },
  });

  const addFiles = useCallback(
    async (incoming: File[]) => {
      if (incoming.length === 0) return;
      if (incoming.some(isArchiveFile)) setProcessing(true);
      const { files: expanded, errors } = await expandArchives(
        incoming,
        (file) => api.subtitles.extractArchive(file),
      );
      if (errors.length > 0) {
        showNotification(
          notification.warn(
            "Could not extract some archives",
            errors.join(", "),
          ),
        );
      }
      if (expanded.length > 0) {
        form.setValues((values) => ({
          ...values,
          files: [...(values.files ?? []), ...expanded.map(buildRow)],
        }));
      }
      setProcessing(false);
    },
    [form, buildRow],
  );

  // Expand any archives among the initially-provided files, once.
  useEffect(() => {
    const archives = files.filter(isArchiveFile);
    if (archives.length > 0) {
      void addFiles(archives);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const action = useArrayAction<SubtitleFile>((fn) => {
    form.setValues((values) => {
      const newFiles = fn(values.files ?? []);
      newFiles.forEach((v) => {
        v.validateResult = validator(v, league.id);
      });
      return { ...values, files: newFiles };
    });
  });

  const ValidateResultCell = ({
    validateResult,
  }: {
    validateResult: SubtitleValidateResult | undefined;
  }) => {
    const icon = useMemo(() => {
      switch (validateResult?.state) {
        case "valid":
          return faCheck;
        case "warning":
          return faInfoCircle;
        case "error":
          return faTimes;
        default:
          return faCircleNotch;
      }
    }, [validateResult?.state]);

    const color = useMemo<MantineColor | undefined>(() => {
      switch (validateResult?.state) {
        case "valid":
          return "green";
        case "warning":
          return "yellow";
        case "error":
          return "red";
        default:
          return undefined;
      }
    }, [validateResult?.state]);

    return (
      <TextPopover text={validateResult?.messages}>
        <Text c={color} inline>
          <FontAwesomeIcon icon={icon}></FontAwesomeIcon>
        </Text>
      </TextPopover>
    );
  };

  const columns = useMemo<ColumnDef<SubtitleFile>[]>(
    () => [
      {
        id: "validateResult",
        cell: ({
          row: {
            original: { validateResult },
          },
        }) => <ValidateResultCell validateResult={validateResult} />,
      },
      {
        header: "File",
        id: "filename",
        accessorKey: "file",
        cell: ({
          row: {
            original: {
              file: { name },
            },
          },
        }) => <Text className="table-primary">{name}</Text>,
      },
      {
        header: () => (
          <Selector
            {...languageOptions}
            value={null}
            placeholder="Language"
            onChange={(value) => {
              if (value) {
                action.update((item) => ({ ...item, language: value }));
              }
            }}
          ></Selector>
        ),
        accessorKey: "language",
        cell: ({ row: { original, index } }) => (
          <Selector
            {...languageOptions}
            className="table-select"
            value={original.language}
            onChange={(item) =>
              action.mutate(index, { ...original, language: item })
            }
          ></Selector>
        ),
      },
      {
        header: () => (
          <Selector
            options={subtitlesTypeOptions}
            value={null}
            placeholder="Type"
            onChange={(value) => {
              if (value) {
                action.update((item) => {
                  switch (value) {
                    case "hi":
                      return { ...item, hi: true, forced: false };
                    case "forced":
                      return { ...item, hi: false, forced: true };
                    case "normal":
                      return { ...item, hi: false, forced: false };
                    default:
                      return item;
                  }
                });
              }
            }}
          ></Selector>
        ),
        accessorKey: "type",
        cell: ({ row: { original, index } }) => (
          <Select
            value={
              subtitlesTypeOptions.find((s) => {
                if (original.hi) return s.value === "hi";
                if (original.forced) return s.value === "forced";
                return s.value === "normal";
              })?.value
            }
            data={subtitlesTypeOptions}
            onChange={(value) => {
              if (value) {
                action.mutate(index, {
                  ...original,
                  hi: value === "hi",
                  forced: value === "forced",
                });
              }
            }}
          ></Select>
        ),
      },
      {
        id: "event",
        header: "Event",
        accessorKey: "event",
        cell: ({ row: { original, index } }) => (
          <Selector
            {...eventOptions}
            searchable
            className="table-select"
            value={original.event}
            onChange={(item) =>
              action.mutate(index, { ...original, event: item })
            }
          ></Selector>
        ),
      },
      {
        id: "action",
        cell: ({ row: { index } }) => (
          <Action
            label="Remove"
            icon={faTrash}
            c="red"
            onClick={() => action.remove(index)}
          ></Action>
        ),
      },
    ],
    [action, eventOptions, languageOptions],
  );

  const { upload } = useSportsSubtitleModification();

  return (
    <form
      onSubmit={form.onSubmit(({ files: rows }) => {
        for (const value of rows) {
          const { file, hi, forced, language, event } = value;
          if (language === null || event === null) {
            throw new Error(
              "Invalid language or event. This shouldn't happen, please report this bug.",
            );
          }
          upload.mutate({
            // The local event id and its own owner, which is what every sports
            // route is keyed on.
            eventId: event.id,
            owner: event.arr_instance_id,
            form: { file, language: language.code2, hi, forced },
          });
        }
        onComplete?.();
        modals.closeSelf();
      })}
    >
      <Stack className="table-long-break">
        <Dropzone
          data-full-page-dropzone-ignore
          onDrop={(dropped) => void addFiles(dropped)}
          loading={processing}
          multiple
        >
          <DropContent></DropContent>
        </Dropzone>
        <SimpleTable columns={columns} data={form.values.files}></SimpleTable>
        <Divider></Divider>
        <Button
          type="submit"
          loading={processing}
          disabled={processing || form.values.files.length === 0}
        >
          Upload
        </Button>
      </Stack>
    </form>
  );
};

export const SportsUploadModal = withModal(
  SportsUploadForm,
  "upload-sports-subtitles",
  { title: "Upload Subtitles", size: "xl" },
);

export default SportsUploadForm;
