import { FunctionComponent, ReactElement, useCallback, useMemo } from "react";
import { Divider, List, Menu, MenuProps, ScrollArea } from "@mantine/core";
import {
  faAlignJustify,
  faClock,
  faClosedCaptioning,
  faCode,
  faExchangeAlt,
  faEye,
  faFaceGrinStars,
  faFilm,
  faImage,
  faLanguage,
  faMagic,
  faPaintBrush,
  faPencil,
  faPlay,
  faSearch,
  faTextHeight,
  faTrash,
  IconDefinition,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSubtitleAction } from "@/apis/hooks";
import { ColorToolModal } from "@/components/forms/ColorToolForm";
import { FrameRateModal } from "@/components/forms/FrameRateForm";
import { TimeOffsetModal } from "@/components/forms/TimeOffsetForm";
import { TranslationModal } from "@/components/forms/TranslationForm";
import { useModals } from "@/modules/modals";
import { ModalComponent } from "@/modules/modals/WithModal";
import { task } from "@/modules/task";
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
  onAction?: (
    action: "delete" | "search" | "view" | "edit" | "compare-sync",
  ) => void;
  canCompareSyncOutputs?: boolean;
  // For missing subtitle translation
  missingLanguage?: Subtitle;
  translationSources?: Subtitle[];
  mediaId?: number;
  mediaType?: "episode" | "movie";
}

const SubtitleToolsMenu: FunctionComponent<Props> = ({
  selections,
  children,
  menu,
  canSync = true,
  onAction,
  canCompareSyncOutputs = false,
  missingLanguage,
  translationSources,
  mediaId,
  mediaType,
}) => {
  const { mutateAsync } = useSubtitleAction();

  const process = useCallback(
    (action: string, name: string) => {
      selections.forEach((s) => {
        const form: FormType.ModifySubtitle = {
          id: s.id,
          type: s.type,
          language: s.language,
          path: s.path,
          hi: s.hi,
          forced: s.forced,
        };
        task.create(s.path, name, mutateAsync, { action, form });
      });
    },
    [mutateAsync, selections],
  );

  const toolGroups = useToolGroups();
  const modals = useModals();

  const disabledTools = selections.length === 0;
  const isMissing = !!missingLanguage;
  const hasSources = (translationSources ?? []).length > 0;

  return (
    <Menu withArrow withinPortal position="left-end" {...menu}>
      <Menu.Target>{children}</Menu.Target>
      <Menu.Dropdown>
        {toolGroups.map((group, groupIdx) => (
          <div key={group.label}>
            {groupIdx > 0 && <Divider />}
            <Menu.Label>{group.label}</Menu.Label>
            {group.tools.map((tool) => {
              if (tool.key === "translation" && isMissing) {
                return null;
              }
              if (tool.key === "sync" && !canSync) {
                return null;
              }
              return (
                <Menu.Item
                  key={tool.key}
                  disabled={disabledTools}
                  leftSection={
                    <FontAwesomeIcon icon={tool.icon}></FontAwesomeIcon>
                  }
                  onClick={() => {
                    if (tool.modal) {
                      modals.openContextModal(tool.modal, { selections });
                    } else {
                      process(tool.key, tool.name);
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
        {/* Translate from source — for missing subtitles */}
        {isMissing && hasSources && (
          <>
            {translationSources!.map((source) => (
              <Menu.Item
                key={`translate-${source.path}`}
                leftSection={<FontAwesomeIcon icon={faLanguage} />}
                onClick={async () => {
                  await mutateAsync({
                    action: "translate",
                    form: {
                      id: mediaId!,
                      type: mediaType!,
                      language: missingLanguage.code2,
                      path: source.path!,
                      forced: toPython(missingLanguage.forced),
                      hi: toPython(missingLanguage.hi),
                    },
                  });
                }}
              >
                Translate from {source.name || source.code2}
                {source.hi ? " (HI)" : ""}
              </Menu.Item>
            ))}
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
          disabled={selections.length === 0 || onAction === undefined}
          leftSection={<FontAwesomeIcon icon={faEye}></FontAwesomeIcon>}
          onClick={() => {
            onAction?.("view");
          }}
        >
          View
        </Menu.Item>
        <Menu.Item
          disabled={onAction === undefined}
          leftSection={<FontAwesomeIcon icon={faPencil}></FontAwesomeIcon>}
          onClick={() => {
            onAction?.("edit");
          }}
        >
          {selections.length === 0 ? "Create / Upload" : "Edit"}
        </Menu.Item>
        <Menu.Item
          disabled={selections.length !== 0 || onAction === undefined}
          leftSection={<FontAwesomeIcon icon={faSearch}></FontAwesomeIcon>}
          onClick={() => {
            onAction?.("search");
          }}
        >
          Search
        </Menu.Item>
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
      </Menu.Dropdown>
    </Menu>
  );
};

export default SubtitleToolsMenu;
