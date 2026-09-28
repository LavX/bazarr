import { FunctionComponent, ReactElement, useCallback, useMemo } from "react";
import {
  Divider,
  List,
  Menu,
  MenuProps,
  ScrollArea,
  Stack,
  Text,
} from "@mantine/core";
import {
  faAlignJustify,
  faClock,
  faClosedCaptioning,
  faCode,
  faDownload,
  faExchangeAlt,
  faEye,
  faEyeSlash,
  faFaceGrinStars,
  faFilm,
  faImage,
  faLanguage,
  faMagic,
  faPaintBrush,
  faPencil,
  faPlay,
  faRotateRight,
  faSearch,
  faTextHeight,
  faTrash,
  IconDefinition,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useSubtitleAction,
  useTranslationAvailability,
  useTranslationRefusal,
} from "@/apis/hooks";
import { ColorToolModal } from "@/components/forms/ColorToolForm";
import { FrameRateModal } from "@/components/forms/FrameRateForm";
import { TimeOffsetModal } from "@/components/forms/TimeOffsetForm";
import { TranslationModal } from "@/components/forms/TranslationForm";
import { useModals } from "@/modules/modals";
import { ModalComponent } from "@/modules/modals/WithModal";
import { toPython } from "@/utilities";
import { SyncSubtitleModal } from "./forms/SyncSubtitleForm";
import { TwoPointFitModal } from "./forms/TwoPointFit";

export interface ToolOptions {
  key: string;
  icon: IconDefinition;
  name: string;
  modal?: ModalComponent<{
    selections: FormType.ModifySubtitle[];
  }>;
}

export interface ToolGroup {
  label: string;
  tools: ToolOptions[];
}

export function useToolGroups() {
  return useMemo<ToolGroup[]>(
    () => [
      {
        label: "Sync & Timing",
        tools: [
          {
            key: "sync",
            icon: faPlay,
            name: "Sync...",
            modal: SyncSubtitleModal,
          },
          {
            key: "change_frame_rate",
            icon: faFilm,
            name: "Change Frame Rate...",
            modal: FrameRateModal,
          },
          {
            key: "adjust_time",
            icon: faClock,
            name: "Adjust Times...",
            modal: TimeOffsetModal,
          },
          {
            key: "two_point_fit",
            icon: faAlignJustify,
            name: "Two-Point Fit...",
            modal: TwoPointFitModal,
          },
        ],
      },
      {
        label: "Cleanup",
        tools: [
          {
            key: "remove_HI",
            icon: faClosedCaptioning,
            name: "Remove HI Tags",
          },
          {
            key: "remove_tags",
            icon: faCode,
            name: "Remove Style Tags",
          },
          {
            key: "emoji",
            icon: faFaceGrinStars,
            name: "Remove Emoji",
          },
          {
            key: "OCR_fixes",
            icon: faImage,
            name: "OCR Fixes",
          },
          {
            key: "common",
            icon: faMagic,
            name: "Common Fixes",
          },
          {
            key: "fix_uppercase",
            icon: faTextHeight,
            name: "Fix Uppercase",
          },
          {
            key: "reverse_rtl",
            icon: faExchangeAlt,
            name: "Reverse RTL",
          },
        ],
      },
      {
        label: "Style & Language",
        tools: [
          {
            key: "add_color",
            icon: faPaintBrush,
            name: "Add Color...",
            modal: ColorToolModal,
          },
          {
            key: "translation",
            icon: faLanguage,
            name: "Translate...",
            modal: TranslationModal,
          },
        ],
      },
    ],
    [],
  );
}

export function useTools() {
  const groups = useToolGroups();
  return useMemo<ToolOptions[]>(() => groups.flatMap((g) => g.tools), [groups]);
}

interface Props {
  selections: FormType.ModifySubtitle[];
  children?: ReactElement;
  menu?: Omit<MenuProps, "children">;
  canSync?: boolean;
  allowSearchExisting?: boolean;
  onAction?: (
    action:
      | "delete"
      | "search"
      | "view"
      | "edit"
      | "compare-sync"
      | "rebuild"
      | "download"
      | "hide",
  ) => void;
  canCompareSyncOutputs?: boolean;
  // When true: hide the editing tool groups (sync, cleanup, style) and show
  // a Rebuild action at the top. Used for combined-subtitle artifact rows.
  isCombinedOutput?: boolean;
  // For missing subtitle translation
  missingLanguage?: Subtitle;
  translationSources?: Subtitle[];
  mediaId?: number;
  mediaType?: "episode" | "movie" | "sports";
  // Owning Sonarr/Radarr instance id (#156) for the missing-subtitle translate
  // path, which builds its ModifySubtitle form explicitly from mediaId/mediaType.
  arrInstanceId?: number;
  /**
   * True when the selection refers to an embedded (in-container) track.
   * Only translate is meaningful; sync/mods require a file on disk, and the
   * red slot hides the track instead of deleting a file.
   */
  embeddedTrack?: boolean;
  /** How the Hide track confirmation names the track, "English (HI)". */
  trackLabel?: string;
  /**
   * Every embedded track the Hide takes, one label each. Tracks are told
   * apart by language, HI and forced only, so an SRT and a PGS English track
   * go together. Defaults to the one track.
   */
  coveredTracks?: string[];
}

const SubtitleToolsMenu: FunctionComponent<Props> = ({
  selections,
  children,
  menu,
  canSync = true,
  allowSearchExisting = false,
  onAction,
  canCompareSyncOutputs = false,
  isCombinedOutput = false,
  missingLanguage,
  translationSources,
  mediaId,
  mediaType,
  arrInstanceId,
  embeddedTrack = false,
  trackLabel,
  coveredTracks,
}) => {
  const { mutate, mutateAsync } = useSubtitleAction();
  // The translate-from items call the mutation directly, so a closed gate has
  // to disable them with its reason: the 409 is reported nowhere else, and a
  // click that fails invisibly is worse than the failed job it replaced.
  const translation = useTranslationAvailability();
  // The disabled state above reads a cached status, which can still say the
  // gate is open when the server answers otherwise: the 409 itself has to
  // show the reason and refetch that status.
  const showTranslationRefusal = useTranslationRefusal();

  const process = useCallback(
    (action: string) => {
      // Each selection is queued as its own backend job, so the request
      // returns at once and the job reports through the jobs drawer.
      selections.forEach((s) => {
        const form: FormType.ModifySubtitle = {
          id: s.id,
          type: s.type,
          language: s.language,
          path: s.path,
          hi: s.hi,
          forced: s.forced,
          // eslint-disable-next-line camelcase
          arr_instance_id: s.arr_instance_id,
        };
        mutate({ action, form });
      });
    },
    [mutate, selections],
  );

  const toolGroups = useToolGroups();
  const modals = useModals();

  const disabledTools = selections.length === 0;
  const isMissing = !!missingLanguage;
  const hasSources = (translationSources ?? []).length > 0;
  // For embedded tracks only translate is supported: no file on disk for other ops
  const isTranslateOnlyMode = embeddedTrack && !isMissing;

  return (
    <Menu withArrow withinPortal position="left-end" {...menu}>
      <Menu.Target>{children}</Menu.Target>
      <Menu.Dropdown>
        {isCombinedOutput && (
          <>
            <Menu.Label>Combined Output</Menu.Label>
            <Menu.Item
              disabled={onAction === undefined}
              leftSection={<FontAwesomeIcon icon={faRotateRight} />}
              onClick={() => onAction?.("rebuild")}
            >
              Rebuild
            </Menu.Item>
            <Divider />
          </>
        )}
        {toolGroups.map((group, groupIdx) => (
          <div key={group.label}>
            {groupIdx > 0 && <Divider />}
            <Menu.Label>{group.label}</Menu.Label>
            {group.tools.map((tool) => {
              // Hide translate from the tools section when showing missing-sub menu
              // (it appears instead in the Actions section as "Translate from X")
              if (tool.key === "translation" && isMissing) {
                return null;
              }
              if (tool.key === "sync" && !canSync) {
                return null;
              }
              // Translate doesn't make sense for combined-output files
              // (the file is already a composition, not a single language).
              if (tool.key === "translation" && isCombinedOutput) {
                return null;
              }
              // For embedded tracks: only translate is supported
              const toolDisabled =
                disabledTools ||
                (isTranslateOnlyMode && tool.key !== "translation");
              return (
                <Menu.Item
                  key={tool.key}
                  disabled={toolDisabled}
                  leftSection={
                    <FontAwesomeIcon icon={tool.icon}></FontAwesomeIcon>
                  }
                  onClick={() => {
                    if (toolDisabled) return;
                    if (tool.modal) {
                      modals.openContextModal(tool.modal, { selections });
                    } else {
                      process(tool.key);
                    }
                  }}
                >
                  {tool.name}
                </Menu.Item>
              );
            })}
          </div>
        ))}
        <Divider></Divider>
        <Menu.Label>Actions</Menu.Label>
        {/* Translate from source, for missing subtitles */}
        {isMissing && hasSources && (
          <>
            {translationSources!.map((source) => {
              const sourceIsEmbedded = !source.path;
              // Use language code as key for embedded tracks (no path)
              const itemKey = `translate-${sourceIsEmbedded ? `embedded:${source.code2}` : source.path}`;
              const label = sourceIsEmbedded
                ? `Translate from ${source.name || source.code2} (embedded)`
                : `Translate from ${source.name || source.code2}${source.hi ? " (HI)" : ""}`;
              return (
                <Menu.Item
                  key={itemKey}
                  disabled={!translation.available}
                  leftSection={<FontAwesomeIcon icon={faLanguage} />}
                  onClick={async () => {
                    try {
                      await mutateAsync({
                        action: "translate",
                        form: {
                          id: mediaId!,
                          type: mediaType!,
                          language: missingLanguage!.code2,
                          // Empty string tells backend to extract the embedded track
                          path: source.path ?? "",
                          forced: toPython(missingLanguage!.forced),
                          hi: toPython(missingLanguage!.hi),
                          from_language: sourceIsEmbedded
                            ? source.code2
                            : undefined,
                          // Identify which embedded source track to extract by its
                          // own hi/forced variant (the source may differ from the
                          // output subtitle's variant).
                          from_hi: sourceIsEmbedded
                            ? toPython(source.hi)
                            : undefined,
                          from_forced: sourceIsEmbedded
                            ? toPython(source.forced)
                            : undefined,
                          // eslint-disable-next-line camelcase
                          arr_instance_id: arrInstanceId,
                        },
                      });
                    } catch (error) {
                      // A stale status can leave the item clickable while the
                      // gate is closed: a 409 says so here. Every other
                      // failure keeps its own toast from the shared client.
                      showTranslationRefusal(error);
                    }
                  }}
                >
                  {/* A disabled item cannot carry a tooltip, so the reason
                      travels as a label suffix instead. */}
                  {translation.available
                    ? label
                    : `${label} (${translation.reason})`}
                </Menu.Item>
              );
            })}
          </>
        )}
        {isMissing && !hasSources && (
          <Menu.Item
            disabled
            leftSection={<FontAwesomeIcon icon={faLanguage} />}
          >
            No source subtitles to translate from
          </Menu.Item>
        )}
        {canCompareSyncOutputs && (
          <Menu.Item
            disabled={onAction === undefined}
            leftSection={
              <FontAwesomeIcon icon={faExchangeAlt}></FontAwesomeIcon>
            }
            onClick={() => {
              onAction?.("compare-sync");
            }}
          >
            Compare Sync Outputs
          </Menu.Item>
        )}
        <Menu.Item
          disabled={
            selections.length === 0 ||
            onAction === undefined ||
            isTranslateOnlyMode
          }
          leftSection={<FontAwesomeIcon icon={faEye}></FontAwesomeIcon>}
          onClick={() => {
            onAction?.("view");
          }}
        >
          View
        </Menu.Item>
        <Menu.Item
          disabled={onAction === undefined || isTranslateOnlyMode}
          leftSection={<FontAwesomeIcon icon={faPencil}></FontAwesomeIcon>}
          onClick={() => {
            onAction?.("edit");
          }}
        >
          {isCombinedOutput || selections.length > 0
            ? "Edit"
            : "Create / Upload"}
        </Menu.Item>
        {/* Embedded tracks have no file on disk to hand out */}
        <Menu.Item
          disabled={
            selections.length === 0 ||
            onAction === undefined ||
            isTranslateOnlyMode
          }
          leftSection={<FontAwesomeIcon icon={faDownload}></FontAwesomeIcon>}
          onClick={() => {
            onAction?.("download");
          }}
        >
          Download
        </Menu.Item>
        {!isCombinedOutput && (
          <Menu.Item
            disabled={
              (selections.length !== 0 && !allowSearchExisting) ||
              onAction === undefined ||
              isTranslateOnlyMode
            }
            leftSection={<FontAwesomeIcon icon={faSearch}></FontAwesomeIcon>}
            onClick={() => {
              onAction?.("search");
            }}
          >
            Search
          </Menu.Item>
        )}
        {isTranslateOnlyMode && (
          // Bazarr+ never rewrites a media file, so on an embedded row the red
          // slot hides the track rather than deleting anything.
          <Menu.Item
            disabled={selections.length === 0 || onAction === undefined}
            color="red"
            leftSection={<FontAwesomeIcon icon={faEyeSlash}></FontAwesomeIcon>}
            onClick={() => {
              const language =
                trackLabel ?? selections[0]?.language ?? "this language";
              const tracks = coveredTracks ?? [language];
              modals.openConfirmModal({
                title: "Hide this embedded track?",
                size: "lg",
                children: (
                  <Stack gap="sm">
                    <Text size="sm">
                      Bazarr+ will stop listing this track for this title and
                      will treat {language} as wanted again. The media file is
                      not changed.
                    </Text>
                    <List>
                      {tracks.map((track, index) => (
                        <List.Item
                          key={`${index}:${track}`}
                        >{`${track} (embedded track)`}</List.Item>
                      ))}
                    </List>
                    {tracks.length > 1 && (
                      <Text size="sm" c="dimmed">
                        Bazarr+ tells embedded tracks apart only by language, HI
                        and forced, so these {tracks.length} tracks are hidden
                        together.
                      </Text>
                    )}
                  </Stack>
                ),
                onConfirm: () => {
                  onAction?.("hide");
                },
                labels: { confirm: "Hide track", cancel: "Cancel" },
                confirmProps: { color: "red" },
              });
            }}
          >
            Hide track...
          </Menu.Item>
        )}
        {!isTranslateOnlyMode && (
          <Menu.Item
            disabled={selections.length === 0 || onAction === undefined}
            color="red"
            leftSection={<FontAwesomeIcon icon={faTrash}></FontAwesomeIcon>}
            onClick={() => {
              modals.openConfirmModal({
                title: "The following subtitles will be deleted",
                size: "lg",
                children: (
                  <ScrollArea style={{ maxHeight: "20rem" }}>
                    <List>
                      {selections.map((s) => (
                        <List.Item my="md" key={s.path}>
                          {s.path}
                        </List.Item>
                      ))}
                    </List>
                  </ScrollArea>
                ),
                onConfirm: () => {
                  onAction?.("delete");
                },
                labels: { confirm: "Delete", cancel: "Cancel" },
                confirmProps: { color: "red" },
              });
            }}
          >
            Delete...
          </Menu.Item>
        )}
      </Menu.Dropdown>
    </Menu>
  );
};

export default SubtitleToolsMenu;
