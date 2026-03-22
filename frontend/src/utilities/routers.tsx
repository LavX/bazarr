import { useCallback, useEffect, useRef } from "react";
import { useBlocker } from "react-router";
import { Button, Group, Stack, Text } from "@mantine/core";
import { modals } from "@mantine/modals";

export function usePrompt(
  when: boolean,
  message: string,
  onSaveAndLeave?: () => Promise<void> | void,
) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      when && currentLocation.pathname !== nextLocation.pathname,
  );

  const prevWhen = useRef(when);
  const previousFocus = useRef<HTMLElement | null>(null);

  const handleStay = useCallback(() => {
    modals.closeAll();
    blocker.reset?.();
    requestAnimationFrame(() => previousFocus.current?.focus());
  }, [blocker]);

  const handleDiscard = useCallback(() => {
    modals.closeAll();
    blocker.proceed?.();
  }, [blocker]);

  const handleSaveAndLeave = useCallback(async () => {
    if (onSaveAndLeave) {
      await onSaveAndLeave();
    }
    modals.closeAll();
    blocker.proceed?.();
  }, [blocker, onSaveAndLeave]);

  useEffect(() => {
    if (blocker.state === "blocked" && prevWhen.current === when) {
      previousFocus.current = document.activeElement as HTMLElement;

      modals.open({
        title: "Unsaved Changes",
        centered: true,
        size: "sm",
        closeOnEscape: true,
        closeOnClickOutside: false,
        onClose: () => {
          if (blocker.state === "blocked") {
            blocker.reset?.();
          }
          requestAnimationFrame(() => previousFocus.current?.focus());
        },
        children: (
          <Stack>
            <Text size="sm" c="dimmed">
              {message}
            </Text>
            <Group justify="flex-end" mt="md" gap="sm">
              <Button
                variant="outline"
                data-autofocus
                onClick={handleStay}
                aria-label="Stay on this page and continue editing"
              >
                Keep Editing
              </Button>
              <Button
                variant="light"
                color="red.9"
                onClick={handleDiscard}
                aria-label="Discard unsaved changes and leave this page"
              >
                Discard Changes
              </Button>
              {onSaveAndLeave && (
                <Button
                  color="brand"
                  onClick={handleSaveAndLeave}
                  aria-label="Save all changes and leave this page"
                >
                  Save & Leave
                </Button>
              )}
            </Group>
          </Stack>
        ),
      });
    }
    prevWhen.current = when;
  }, [blocker, message, when, handleStay, handleDiscard, handleSaveAndLeave, onSaveAndLeave]);
}
