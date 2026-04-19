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
    if (blocker.state === "blocked") {
      blocker.reset?.();
    }
    requestAnimationFrame(() => previousFocus.current?.focus());
  }, [blocker]);

  const handleDiscard = useCallback(() => {
    if (blocker.state === "blocked") {
      blocker.proceed?.();
    }
    modals.closeAll();
  }, [blocker]);

  const handleSaveAndLeave = useCallback(async () => {
    if (onSaveAndLeave) {
      await onSaveAndLeave();
    }
    if (blocker.state === "blocked") {
      blocker.proceed?.();
    }
    modals.closeAll();
  }, [blocker, onSaveAndLeave]);

  useEffect(() => {
    if (blocker.state === "blocked" && prevWhen.current === when) {
      previousFocus.current = document.activeElement as HTMLElement;

      modals.open({
        title: "Unsaved Changes",
        centered: true,
        size: "md",
        closeOnEscape: true,
        closeOnClickOutside: false,
        onClose: () => {
          // Only reset if still blocked (not if proceed was already called)
          if (blocker.state === "blocked") {
            blocker.reset?.();
            requestAnimationFrame(() => previousFocus.current?.focus());
          }
        },
        children: (
          <Stack>
            <Text size="sm" c="var(--bz-text-tertiary)">
              {message}
            </Text>
            <Group justify="flex-end" mt="lg" gap="xs">
              <Button
                variant="default"
                size="sm"
                data-autofocus
                onClick={handleStay}
                aria-label="Stay on this page and continue editing"
              >
                Keep Editing
              </Button>
              <Button
                variant="light"
                color="red"
                size="sm"
                onClick={handleDiscard}
                aria-label="Discard unsaved changes and leave this page"
              >
                Discard
              </Button>
              {onSaveAndLeave && (
                <Button
                  color="brand"
                  size="sm"
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
  }, [
    blocker,
    message,
    when,
    handleStay,
    handleDiscard,
    handleSaveAndLeave,
    onSaveAndLeave,
  ]);
}
