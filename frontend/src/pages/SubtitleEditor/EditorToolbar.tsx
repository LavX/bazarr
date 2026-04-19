import { ActionIcon, Badge, Group, Tooltip } from "@mantine/core";
import {
  faBook,
  faBookmark,
  faClipboardCheck,
  faClock,
  faCopy,
  faDownload,
  faKeyboard,
  faLanguage,
  faLink,
  faPlus,
  faRedo,
  faSave,
  faScissors,
  faSearch,
  faSpinner,
  faTrash,
  faUndo,
  faUpload,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

type ToolbarAction =
  | "save"
  | "saveAs"
  | "upload"
  | "download"
  | "undo"
  | "redo"
  | "search"
  | "qc"
  | "timing"
  | "translate"
  | "reference"
  | "addCue"
  | "split"
  | "merge"
  | "delete";

interface EditorToolbarProps {
  onAction: (action: ToolbarAction) => void;
  dirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  format: string;
  language: string;
  translating?: boolean;
  referenceActive?: boolean;
  bookmarkFilterActive?: boolean;
  bookmarkCount?: number;
  onToggleBookmarkFilter?: () => void;
  onShowShortcuts?: () => void;
}

function Divider() {
  return (
    <div
      style={{
        width: 1,
        height: 20,
        background: "var(--bz-border-interactive)",
        flexShrink: 0,
      }}
    />
  );
}

export default function EditorToolbar({
  onAction,
  dirty,
  canUndo,
  canRedo,
  format,
  language,
  translating,
  referenceActive,
  bookmarkFilterActive,
  bookmarkCount = 0,
  onToggleBookmarkFilter,
  onShowShortcuts,
}: EditorToolbarProps) {
  return (
    <Group
      role="toolbar"
      aria-label="Editor toolbar"
      aria-orientation="horizontal"
      gap="xs"
      wrap="nowrap"
      justify="space-between"
      style={{
        height: 40,
        background: "var(--bz-surface-base)",
        borderBottom: "1px solid var(--bz-border-card)",
        padding: "0 12px",
        flexShrink: 0,
      }}
    >
      <Group gap="xs" wrap="nowrap">
        {/* File ops */}
        <Group gap={4} wrap="nowrap">
          <Tooltip label="Save (Ctrl+S)" position="bottom" withArrow>
            <div style={{ position: "relative", display: "inline-flex" }}>
              <ActionIcon
                size="sm"
                variant="subtle"
                color="gray"
                onClick={() => onAction("save")}
                aria-label="Save"
              >
                <FontAwesomeIcon icon={faSave} />
              </ActionIcon>
              {dirty && (
                <div
                  style={{
                    position: "absolute",
                    top: 2,
                    right: 2,
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: "#f59e0b",
                    pointerEvents: "none",
                  }}
                />
              )}
            </div>
          </Tooltip>
          <Tooltip label="Upload (Ctrl+O)" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("upload")}
              aria-label="Upload"
            >
              <FontAwesomeIcon icon={faUpload} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Download" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("download")}
              aria-label="Download"
            >
              <FontAwesomeIcon icon={faDownload} />
            </ActionIcon>
          </Tooltip>
          <Tooltip
            label="Save As (different language)"
            position="bottom"
            withArrow
          >
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("saveAs")}
              aria-label="Save As"
            >
              <FontAwesomeIcon icon={faCopy} />
            </ActionIcon>
          </Tooltip>
        </Group>

        <Divider />

        {/* Edit ops */}
        <Group gap={4} wrap="nowrap">
          <Tooltip label="Undo (Ctrl+Z)" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              disabled={!canUndo}
              onClick={() => onAction("undo")}
              aria-label="Undo"
            >
              <FontAwesomeIcon icon={faUndo} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Redo (Ctrl+Y)" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              disabled={!canRedo}
              onClick={() => onAction("redo")}
              aria-label="Redo"
            >
              <FontAwesomeIcon icon={faRedo} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Search/Replace (Ctrl+F)" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("search")}
              aria-label="Search/Replace"
            >
              <FontAwesomeIcon icon={faSearch} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Quality Control" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("qc")}
              aria-label="Quality Control"
            >
              <FontAwesomeIcon icon={faClipboardCheck} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Timing Tools" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("timing")}
              aria-label="Timing Tools"
            >
              <FontAwesomeIcon icon={faClock} />
            </ActionIcon>
          </Tooltip>
        </Group>

        <Divider />

        {/* Cue ops */}
        <Group gap={4} wrap="nowrap">
          <Tooltip
            label="Add Cue (Ctrl+Shift+Enter)"
            position="bottom"
            withArrow
          >
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("addCue")}
              aria-label="Add Cue"
            >
              <FontAwesomeIcon icon={faPlus} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Split Cue (Ctrl+Shift+S)" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("split")}
              aria-label="Split Cue"
            >
              <FontAwesomeIcon icon={faScissors} />
            </ActionIcon>
          </Tooltip>
          <Tooltip
            label="Merge Cues (Ctrl+Shift+M)"
            position="bottom"
            withArrow
          >
            <ActionIcon
              size="sm"
              variant="subtle"
              color="gray"
              onClick={() => onAction("merge")}
              aria-label="Merge Cues"
            >
              <FontAwesomeIcon icon={faLink} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Delete Cue (Ctrl+Del)" position="bottom" withArrow>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="red"
              onClick={() => onAction("delete")}
              aria-label="Delete Cue"
            >
              <FontAwesomeIcon icon={faTrash} />
            </ActionIcon>
          </Tooltip>
        </Group>

        <Divider />

        {/* Reference + AI ops */}
        <Group gap={4} wrap="nowrap">
          <Tooltip
            label="Reference Subtitle (Ctrl+Shift+R)"
            position="bottom"
            withArrow
          >
            <ActionIcon
              size="sm"
              variant="subtle"
              color={referenceActive ? "teal" : "gray"}
              onClick={() => onAction("reference")}
              aria-label="Reference Subtitle"
            >
              <FontAwesomeIcon icon={faBook} />
            </ActionIcon>
          </Tooltip>
          <Tooltip
            label="AI Translate (Ctrl+Shift+T)"
            position="bottom"
            withArrow
          >
            <ActionIcon
              size="sm"
              variant="subtle"
              color={translating ? "violet" : "gray"}
              onClick={() => onAction("translate")}
              aria-label="AI Translate"
            >
              <FontAwesomeIcon
                icon={translating ? faSpinner : faLanguage}
                spin={translating}
              />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      {/* Right side: filters, shortcuts, badges */}
      <Group gap="xs" wrap="nowrap">
        <Tooltip
          label={
            bookmarkCount > 0
              ? `Show bookmarked only (${bookmarkCount})`
              : "No bookmarks (Ctrl+B to bookmark)"
          }
          position="bottom"
          withArrow
        >
          <ActionIcon
            size="sm"
            variant="subtle"
            color={bookmarkFilterActive ? "yellow" : "gray"}
            onClick={onToggleBookmarkFilter}
            disabled={bookmarkCount === 0}
            aria-label="Filter bookmarked"
            aria-pressed={bookmarkFilterActive}
          >
            <FontAwesomeIcon icon={faBookmark} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Keyboard shortcuts (?)" position="bottom" withArrow>
          <ActionIcon
            size="sm"
            variant="subtle"
            color="gray"
            onClick={onShowShortcuts}
            aria-label="Keyboard shortcuts"
          >
            <FontAwesomeIcon icon={faKeyboard} />
          </ActionIcon>
        </Tooltip>
        <Divider />
        <Badge size="sm" variant="filled" color="dark" radius="sm">
          {format.toUpperCase()}
        </Badge>
        <Badge size="sm" variant="filled" color="dark" radius="sm">
          {language}
        </Badge>
      </Group>
    </Group>
  );
}
