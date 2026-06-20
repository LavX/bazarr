import {
  FunctionComponent,
  RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { Box, getDefaultZIndex, Group, Stack, Text } from "@mantine/core";
import { faFileCirclePlus } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

interface FullPageDropzoneProps {
  // Only listen while the page is ready to accept uploads (e.g. a language
  // profile is set). When false the component is inert.
  active: boolean;
  onDrop: (files: File[]) => void;
  // Assigned a function that opens the native file picker, for a toolbar button.
  openRef?: RefObject<(() => void) | null>;
}

function dragHasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

// Self-contained overlay content. NOT Mantine's DropContent, which uses
// Dropzone.Idle/Accept/Reject and therefore must live inside a <Dropzone>
// context - rendering it here would crash with "Dropzone component was not
// found in tree".
const Overlay: FunctionComponent = () => (
  <Group justify="center" gap="xl">
    <FontAwesomeIcon icon={faFileCirclePlus} size="2x" />
    <Stack gap={0}>
      <Text size="lg">Upload Subtitles</Text>
      <Text c="var(--bz-text-tertiary)" size="sm">
        Drop subtitle files or a .zip / .rar / .7z archive to upload
      </Text>
    </Stack>
  </Group>
);

// A reliable full-window file dropzone. Unlike Mantine's Dropzone.FullScreen -
// whose overlay races the browser's native dragover/drop and lets the browser
// open the dropped file instead - this captures drag events at the window level
// and always preventDefault()s them, so a file dropped anywhere on the page is
// handed to onDrop. It also drives a hidden file input via openRef so the same
// path backs a toolbar "Upload" button.
export const FullPageDropzone: FunctionComponent<FullPageDropzoneProps> = ({
  active,
  onDrop,
  openRef,
}) => {
  const [visible, setVisible] = useState(false);
  const depth = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!openRef) {
      return;
    }
    openRef.current = () => inputRef.current?.click();
    return () => {
      openRef.current = null;
    };
  }, [openRef]);

  useEffect(() => {
    if (!active) {
      depth.current = 0;
      setVisible(false);
      return;
    }

    const onDragEnter = (event: DragEvent) => {
      if (!dragHasFiles(event)) {
        return;
      }
      depth.current += 1;
      setVisible(true);
    };
    // Prevent default on dragover so the browser allows the drop everywhere
    // rather than treating it as navigation to the file.
    const onDragOver = (event: DragEvent) => {
      if (dragHasFiles(event)) {
        event.preventDefault();
      }
    };
    const onDragLeave = () => {
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) {
        setVisible(false);
      }
    };
    const onWindowDrop = (event: DragEvent) => {
      if (!dragHasFiles(event)) {
        return;
      }
      event.preventDefault();
      depth.current = 0;
      setVisible(false);
      const files = Array.from(event.dataTransfer?.files ?? []);
      if (files.length > 0) {
        onDrop(files);
      }
    };

    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onWindowDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onWindowDrop);
    };
  }, [active, onDrop]);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        style={{ display: "none" }}
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          if (files.length > 0) {
            onDrop(files);
          }
        }}
      />
      {visible && (
        <Box
          style={{
            position: "fixed",
            inset: 0,
            zIndex: getDefaultZIndex("max"),
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            backgroundColor: "var(--mantine-color-body)",
            opacity: 0.92,
            // The window listeners own the drop; the overlay is purely visual.
            pointerEvents: "none",
          }}
        >
          <Overlay />
        </Box>
      )}
    </>
  );
};
