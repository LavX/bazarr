import {
  FormEvent,
  FunctionComponent,
  useCallback,
  useRef,
  useState,
} from "react";
import { Button, Group, PasswordInput, Stack, Text } from "@mantine/core";
import { useSystemSettings } from "@/apis/hooks";
import api from "@/apis/raw";
import { useModals, withModal } from "@/modules/modals";
import {
  markPasswordConfirmed,
  passwordConfirmWindowLive,
} from "@/modules/passwordConfirm";

export interface PasswordConfirmViewProps {
  /** The gated action, named so the prompt says what the confirmation unlocks. */
  action: string;
  /** Invoked once the server accepted the confirmation. */
  onConfirmed: () => void;
  /** Shown in the error slot on open, when the settings read never answered. */
  initialError?: string;
}

export const PasswordConfirmView: FunctionComponent<
  PasswordConfirmViewProps
> = ({ action, onConfirmed, initialError }) => {
  const { closeSelf } = useModals();
  const { data: settings } = useSystemSettings();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | undefined>(initialError);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(undefined);
    try {
      await api.system.confirmPassword(
        settings?.auth?.username ?? "",
        password,
      );
      markPasswordConfirmed();
      onConfirmed();
      closeSelf();
    } catch (error) {
      const status = (error as { response?: { status?: number } })?.response
        ?.status;
      if (status === 403) {
        setError("The password is not correct.");
      } else if (status === 429) {
        setError("Too many attempts. Wait five minutes and try again.");
      }
      // Any other failure was already reported by the shared client.
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit}>
      <Stack gap="sm">
        <Text size="sm" c="var(--bz-text-secondary)">
          Confirm your password to {action}. The confirmation is remembered for
          ten minutes.
        </Text>
        <PasswordInput
          label="Password"
          placeholder="Your password"
          value={password}
          onChange={(event) => setPassword(event.currentTarget.value)}
          error={error}
          data-autofocus
        />
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" onClick={closeSelf}>
            Cancel
          </Button>
          <Button type="submit" loading={submitting}>
            Confirm
          </Button>
        </Group>
      </Stack>
    </form>
  );
};

export const PasswordConfirmModal = withModal(
  PasswordConfirmView,
  "password-confirm",
  {
    title: "Confirm your password",
  },
);

/**
 * Run one action the backend gates behind a password confirmation.
 *
 * With no login mode configured there is no password to confirm, and a
 * confirmation inside its ten-minute window is remembered, so the prompt
 * appears only when the server would refuse the action without it.
 */
export function useGatedAction() {
  const modals = useModals();
  const settingsQuery = useSystemSettings();

  // Latest-value refs keep the returned callback identity-stable. The
  // unsaved-changes prompt depends on the save path, so an unstable callback
  // would churn that prompt's effect on every render while it is open.
  const latest = useRef({ modals, query: settingsQuery });
  latest.current = { modals, query: settingsQuery };

  return useCallback(
    async (
      run: () => void | Promise<void>,
      action: string,
    ): Promise<boolean> => {
      const { modals: modalManager, query } = latest.current;
      let authType = query.data?.auth?.type;
      if (query.data === undefined) {
        // Nothing has answered about the settings yet, and an unanswered read
        // is not a no-login answer: the setup wizard sits outside the app's
        // settings-loading guard, so the catalog can be ready while the
        // settings are still loading, and a batch that ran here would submit
        // without the prompt and land on a 403. Waiting for the read settles
        // that order, and refetch also retries one that failed.
        const settled = await query.refetch();
        if (settled.data === undefined) {
          // A second unanswered read means the gate cannot decide, and the
          // action stays blocked: running it anyway would let the wizard
          // submit the install and meet its 403 with no prompt on the way.
          // The modal carries the failure in its error slot instead, and
          // invoking the gated action again repeats the read, so the error
          // stays retryable until the auth mode is known.
          await new Promise<void>((resolve) => {
            modalManager.openContextModal(
              PasswordConfirmModal,
              {
                action,
                initialError:
                  "Bazarr+ could not read its settings, so it cannot tell whether a password is needed. Try again.",
                onConfirmed: () => resolve(),
              },
              { onClose: () => resolve() },
            );
          });
          return false;
        }
        authType = settled.data?.auth?.type;
      }
      if (authType !== "form" && authType !== "basic") {
        await run();
        return true;
      }
      if (passwordConfirmWindowLive()) {
        await run();
        return true;
      }
      const confirmed = await new Promise<boolean>((resolve) => {
        modalManager.openContextModal(
          PasswordConfirmModal,
          { action, onConfirmed: () => resolve(true) },
          { onClose: () => resolve(false) },
        );
      });
      if (confirmed) {
        await run();
      }
      return confirmed;
    },
    // Empty on purpose: the latest-value refs above carry the values, and the
    // callback's identity must never change across renders.
    [],
  );
}
