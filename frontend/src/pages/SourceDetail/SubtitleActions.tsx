import { useState } from "react";
import { Alert, Menu, Stack, Text } from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import {
  faEllipsis,
  faFileExcel,
  faLayerGroup,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import queryClient from "@/apis/queries";
import { expireSourceSubtitleDocument } from "@/apis/queries/sourceSubtitleDocument";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { Action, Toolbox } from "@/components";
import { CombineModal } from "@/components/forms/CombineForm";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { useModals } from "@/modules/modals";
import { notification } from "@/modules/notification";
import { subtitleEditorUrl } from "@/pages/SubtitleEditor/editorScope";
import { toPython } from "@/utilities";
import { basename, saveBlobAs } from "@/utilities/files";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  combineRequestForSubtitle,
  isCombinedOutputSubtitle,
  isSyncOutputSubtitle,
} from "@/utilities/subtitles";
import {
  sourceDetailApi,
  sourceError,
  SourceItem,
  useSourceWrite,
} from "./api";
import {
  sourceHistoryApi,
  sourceReleaseForSubtitle,
  useSourceHistory,
} from "./History";

export type OpenSourceEditor = (path: string) => void;

async function requireCurrentSubtitle(
  item: SourceItem,
  subtitle: Subtitle,
  readByLanguage?: boolean,
) {
  const current = await sourceDetailApi.requireCurrentItem(item, subtitle);
  if (
    readByLanguage &&
    current.subtitles.some(
      (row) =>
        row.path &&
        row.path !== subtitle.path &&
        buildSubtitleLanguageKey(row) === buildSubtitleLanguageKey(subtitle),
    )
  ) {
    throw new Error("More than one file is listed for this subtitle language.");
  }
}

export function sourceSubtitleSelection(
  item: SourceItem,
  subtitle: Subtitle,
): FormType.ModifySubtitle {
  return {
    type: "source",
    id: item.id,
    path: subtitle.path ?? "",
    language: subtitle.code2,
    hi: toPython(subtitle.hi),
    forced: toPython(subtitle.forced),
    sourceLanguage: buildSubtitleLanguageKey(subtitle),
    validateSource: (readByLanguage) =>
      requireCurrentSubtitle(item, subtitle, readByLanguage),
  };
}

export function canSynchronizeSourceSubtitle(subtitle: Subtitle) {
  return !subtitle.forced && canSynchronizeSubtitle(subtitle);
}

function combineInputs(item: SourceItem) {
  return item.subtitles.flatMap((row) =>
    row.path?.toLowerCase().endsWith(".srt") &&
    !isSyncOutputSubtitle(row) &&
    !isCombinedOutputSubtitle(row)
      ? [{ path: row.path, language: buildSubtitleLanguageKey(row) }]
      : [],
  );
}

function combineScope(item: SourceItem, output?: Subtitle) {
  return {
    kind: "source" as const,
    itemId: item.id,
    validate: async (body: Api.CombineRequest) => {
      const current = await sourceDetailApi.requireCurrentItem(item, output);
      const available = combineInputs(current);
      if (
        !body.inputs ||
        body.inputs.length !== body.languages?.length ||
        body.inputs.some(
          (input, index) =>
            input.language.split(":")[0] !== body.languages?.[index] ||
            !available.some(
              (row) =>
                row.path === input.path && row.language === input.language,
            ),
        )
      ) {
        throw new Error(
          "A selected subtitle is no longer listed for this item.",
        );
      }
    },
  };
}

function rebuildInputs(
  item: SourceItem,
  subtitle: Subtitle,
  languages: string[],
) {
  const modifiers = buildSubtitleLanguageKey(subtitle).split(":").slice(1);
  const primaryVariants = [
    ...(subtitle.hi || modifiers.includes("hi") ? ["hi"] : []),
    ...(subtitle.forced || modifiers.includes("forced") ? ["forced"] : []),
  ];
  return languages.flatMap((language, index) => {
    const exactLanguage = [language, ...primaryVariants].join(":");
    const candidates = combineInputs(item).filter((row) =>
      index === 0 && primaryVariants.length > 0
        ? row.language === exactLanguage
        : row.language.split(":")[0] === language,
    );
    return candidates.length === 1 ? candidates : [];
  });
}

function useSourceCombineAction(item: SourceItem) {
  const modals = useModals();
  const sourceInputs = combineInputs(item);
  const availableLanguages = [
    ...new Set(sourceInputs.map((row) => row.language.split(":")[0])),
  ];
  return {
    canCombine: availableLanguages.length >= 2,
    openCombine: () =>
      modals.openContextModal(CombineModal, {
        scope: combineScope(item),
        availableLanguages,
        sourceInputs,
      }),
  };
}

export function SourceCombineButton({
  item,
  disabled,
}: {
  item: SourceItem;
  disabled?: boolean;
}) {
  const { canCombine, openCombine } = useSourceCombineAction(item);
  return (
    <Toolbox.Button
      icon={faLayerGroup}
      disabled={disabled || !canCombine}
      onClick={openCombine}
    >
      Combine Subtitles
    </Toolbox.Button>
  );
}

export function SourceSubtitleMenu({
  item,
  subtitle,
  openEditor,
}: {
  item: SourceItem;
  subtitle: Subtitle;
  openEditor: OpenSourceEditor;
}) {
  const modals = useModals();
  const write = useSourceWrite({ notifyError: true });
  const rebuild = useCombineSubtitles();
  const { canCombine, openCombine } = useSourceCombineAction(item);
  const [opened, setOpened] = useState(false);
  const [fileError, setFileError] = useState<string>();
  const [checkingRebuild, setCheckingRebuild] = useState(false);
  const history = useSourceHistory(item, opened);
  const release = sourceReleaseForSubtitle(
    history.data?.data ?? [],
    item,
    subtitle,
  );
  const combined = isCombinedOutputSubtitle(subtitle);
  const language = buildSubtitleLanguageKey(subtitle);
  const readFile = async (action: "view" | "edit" | "download") => {
    setFileError(undefined);
    try {
      await requireCurrentSubtitle(item, subtitle, true);
      if (action === "download") {
        const { data } = await api.subtitles.getContent(
          "source",
          item.id,
          language,
        );
        saveBlobAs(
          new Blob([data.content], { type: "text/plain;charset=utf-8" }),
          basename(subtitle.path ?? "subtitle.srt"),
        );
        return;
      }
      await expireSourceSubtitleDocument(queryClient, item.id, language);
      modals.closeAll();
      openEditor(
        subtitleEditorUrl(
          action === "view" ? "preview" : "edit",
          "source",
          item.id,
          language,
        ),
      );
    } catch (error) {
      setFileError(sourceError(error));
    }
  };
  const rebuildFile = async () => {
    const body = combineRequestForSubtitle(subtitle);
    if (!body) return;
    setFileError(undefined);
    setCheckingRebuild(true);
    try {
      const current = await sourceDetailApi.requireCurrentItem(item, subtitle);
      const previousInputs = rebuildInputs(item, subtitle, body.languages);
      const inputs = rebuildInputs(current, subtitle, body.languages).filter(
        (input) => {
          const previous = previousInputs.find(
            (row) =>
              row.language.split(":")[0] === input.language.split(":")[0],
          );
          return (
            previous?.path === input.path &&
            previous.language === input.language
          );
        },
      );
      if (inputs.length !== body.languages.length) {
        const sourceInputs = combineInputs(current);
        modals.openContextModal(CombineModal, {
          scope: combineScope(current, subtitle),
          availableLanguages: [
            ...new Set(
              sourceInputs.map((input) => input.language.split(":")[0]),
            ),
          ],
          sourceInputs,
          initialRequest: { ...body, inputs },
        });
        return;
      }
      const result = await rebuild.mutateAsync({
        scope: combineScope(current, subtitle),
        body: { ...body, inputs },
      });
      if (result.status === "built" && !result.error) {
        showNotification(
          notification.info(
            "Combined subtitle generated",
            `Saved to ${result.path}`,
          ),
        );
      } else if (result.status !== "queued") {
        showNotification(
          notification.warn(
            "Combine did not complete",
            result.error ?? result.reason ?? "",
          ),
        );
      }
    } catch (error) {
      setFileError(sourceError(error));
    } finally {
      setCheckingRebuild(false);
    }
  };

  return (
    <Stack gap="xs">
      <SubtitleToolsMenu
        selections={[sourceSubtitleSelection(item, subtitle)]}
        menu={{ onChange: setOpened }}
        canSync={canSynchronizeSourceSubtitle(subtitle)}
        isCombinedOutput={combined}
        onAction={(action) => {
          if (action === "view" || action === "edit" || action === "download") {
            void readFile(action);
          } else if (action === "delete") {
            write.mutate(async () => {
              await sourceDetailApi.requireCurrentItem(item, subtitle);
              return sourceDetailApi.removeSubtitle(item.id, subtitle);
            });
          } else if (action === "rebuild") {
            void rebuildFile();
          }
        }}
        extraActions={
          <>
            {!combined && canCombine && (
              <Menu.Item
                leftSection={<FontAwesomeIcon icon={faLayerGroup} />}
                onClick={openCombine}
              >
                Combine Subtitles...
              </Menu.Item>
            )}
            {release && (
              <Menu.Item
                color="red"
                leftSection={<FontAwesomeIcon icon={faFileExcel} />}
                onClick={() =>
                  modals.openConfirmModal({
                    title: "Exclude from search?",
                    children: (
                      <Text size="sm">
                        Delete {basename(subtitle.path ?? "")} and exclude this
                        release from future searches.
                      </Text>
                    ),
                    labels: {
                      confirm: "Exclude from search",
                      cancel: "Cancel",
                    },
                    confirmProps: { color: "red" },
                    onConfirm: () =>
                      write.mutate(async () => {
                        const current =
                          await sourceDetailApi.requireCurrentItem(
                            item,
                            subtitle,
                          );
                        const { data } = await withInlineErrors(() =>
                          sourceHistoryApi.history(item.id),
                        );
                        const latest = sourceReleaseForSubtitle(
                          data,
                          current,
                          subtitle,
                        );
                        if (latest?.history_id !== release.history_id) {
                          throw new Error(
                            "The subtitle release changed. Reopen its actions to try again.",
                          );
                        }
                        return withInlineErrors(() =>
                          sourceHistoryApi.exclude(item.id, latest),
                        );
                      }),
                  })
                }
              >
                Exclude from search...
              </Menu.Item>
            )}
          </>
        }
      >
        <Action
          label="Actions"
          icon={faEllipsis}
          disabled={write.isPending || rebuild.isPending || checkingRebuild}
        />
      </SubtitleToolsMenu>
      {fileError && <Alert color="red">{fileError}</Alert>}
    </Stack>
  );
}
