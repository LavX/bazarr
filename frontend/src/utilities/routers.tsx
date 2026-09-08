import { useCallback, useEffect, useId, useRef } from "react";
import { useBlocker } from "react-router";
import { Button, Group, Stack, Text } from "@mantine/core";
import { modals } from "@mantine/modals";

export function usePrompt(
  when: boolean,
  message: string,
  onSaveAndLeave?: () => Promise<void> | void,
  savedRefreshFailed = false,
) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      when && currentLocation.pathname !== nextLocation.pathname,
  );

  const prevWhen = useRef(when);
  const previousFocus = useRef<HTMLElement | null>(null);
  const modalId = useId();
  const promptOpen = useRef(false);

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
      try {
        await onSaveAndLeave();
      } catch {
        // The settings mutation reports the failure. Keep the draft and blocker.
        return;
      }
    }
    if (blocker.state === "blocked") {
      blocker.proceed?.();
    }
    modals.closeAll();
  }, [blocker, onSaveAndLeave]);

  useEffect(() => {
    if (blocker.state !== "blocked") promptOpen.current = false;
    if (blocker.state === "blocked" && prevWhen.current === when) {
      if (!promptOpen.current)
        previousFocus.current = document.activeElement as HTMLElement;

      const options = {
        modalId,
        title: savedRefreshFailed
          ? "Application refresh failed"
          : "Unsaved Changes",
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
                aria-label={
                  savedRefreshFailed
                    ? "Leave this page keeping saved settings"
                    : "Discard unsaved changes and leave this page"
                }
              >
                {savedRefreshFailed ? "Leave with saved settings" : "Discard"}
              </Button>
              {onSaveAndLeave && (
                <Button
                  color="brand"
                  size="sm"
                  onClick={handleSaveAndLeave}
                  aria-label={
                    savedRefreshFailed
                      ? "Retry application refresh and leave this page"
                      : "Save all changes and leave this page"
                  }
                >
                  {savedRefreshFailed
                    ? "Retry refresh & Leave"
                    : "Save & Leave"}
                </Button>
              )}
            </Group>
          </Stack>
        ),
      };
      if (promptOpen.current) {
        modals.updateModal(options);
      } else {
        modals.open(options);
        promptOpen.current = true;
      }
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
    savedRefreshFailed,
    modalId,
  ]);
}
