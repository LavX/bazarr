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
        // The dialog's own controls carry the same 44px touch floor as the page
        // behind it. Mantine's sm button is 36px and its close control smaller
        // still, and at 320px these are the only way out of the prompt.
        closeButtonProps: { size: 44 },
        styles: { close: { minWidth: 44, minHeight: 44 } },
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
              {/* The visible label carries the meaning, and there is no
                  aria-label overriding it. A reader using voice control says
                  the words on the button, so an accessible name that does not
                  contain them does not activate it (WCAG 2.5.3, Level A). */}
              <Button
                variant="default"
                size="sm"
                mih={44}
                data-autofocus
                onClick={handleStay}
              >
                Keep editing
              </Button>
              <Button
                variant="light"
                color="red"
                size="sm"
                mih={44}
                onClick={handleDiscard}
              >
                {savedRefreshFailed
                  ? "Leave with saved settings"
                  : "Discard changes"}
              </Button>
              {onSaveAndLeave && (
                <Button
                  color="brand"
                  size="sm"
                  mih={44}
                  onClick={handleSaveAndLeave}
                >
                  {savedRefreshFailed
                    ? "Retry refresh and leave"
                    : "Save and leave"}
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
