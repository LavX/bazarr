import { useEffect, useRef, useState } from "react";
import { Alert, Button, Paper, Stack, Text, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { mediaServerLinksKey } from "@/apis/hooks/mediaServerLinks";
import {
  usePlexAuthValidationQuery,
  usePlexLogoutMutation,
  usePlexPinCheckQuery,
  usePlexPinMutation,
} from "@/apis/hooks/plex";
import { QueryKeys } from "@/apis/queries/keys";
import { PLEX_AUTH_CONFIG } from "@/constants/plex";
import { useModals } from "@/modules/modals";
import styles from "@/pages/Settings/Plex/AuthSection.module.scss";

// The backend's own words for a failed request, when it gave any.
function reasonOf(error: unknown): string | undefined {
  if (!(error instanceof AxiosError)) {
    return undefined;
  }
  const data = error.response?.data as { message?: unknown } | undefined;
  return typeof data?.message === "string" ? data.message : undefined;
}

const AuthSection = () => {
  const {
    data: authData,
    isLoading: authIsLoading,
    error: authError,
    refetch: refetchAuth,
  } = usePlexAuthValidationQuery();
  const { mutateAsync: createPin } = usePlexPinMutation();
  // The failure is said below in a notification of its own, so the client's
  // generic "Error <code>" toast stays away.
  const { mutate: logout, isPending: isLoggingOut } = usePlexLogoutMutation({
    inlineErrors: true,
  });
  const [pin, setPin] = useState<Plex.Pin | null>(null);
  const authWindowRef = useRef<Window | null>(null);
  const queryClient = useQueryClient();
  const modals = useModals();

  const isPolling = !!pin?.pinId;

  const { data: pinData } = usePlexPinCheckQuery(
    pin?.pinId ?? null,
    pin?.state ?? null,
    isPolling,
    pin?.pinId ? PLEX_AUTH_CONFIG.POLLING_INTERVAL_MS : false,
  );

  // Handle successful authentication - stop polling and close window
  useEffect(() => {
    if (pinData?.authenticated && isPolling) {
      setPin(null);
      if (authWindowRef.current) {
        authWindowRef.current.close();
        authWindowRef.current = null;
      }
      // Trigger refetch and invalidate server queries
      void refetchAuth();
      void queryClient.invalidateQueries({
        queryKey: [QueryKeys.Plex, "servers"],
      });
      void queryClient.invalidateQueries({
        queryKey: [QueryKeys.Plex, "selectedServer"],
      });
      // Signing in switches the account's destination back on, so a Play on
      // menu can offer it again.
      void queryClient.invalidateQueries({ queryKey: mediaServerLinksKey });
    }
  }, [pinData?.authenticated, isPolling, refetchAuth, queryClient]);

  const isAuthenticated = Boolean(
    authData?.valid && authData?.auth_method === "oauth",
  );

  const handleAuth = async () => {
    // Every branch below used to fall through to `pin.authUrl`, so a Plex API
    // that was unreachable, rate-limited or simply answered without a pin threw
    // an unhandled rejection and the button did nothing at all.
    let pin: Plex.Pin | null = null;
    try {
      pin = (await createPin()).data ?? null;
    } catch (error) {
      notifications.show({
        title: "Could not reach Plex",
        message:
          error instanceof Error
            ? error.message
            : "Plex did not answer the sign-in request. Try again in a moment.",
        color: "red",
      });
      return;
    }

    if (!pin?.authUrl) {
      notifications.show({
        title: "Could not start Plex sign-in",
        message: "Plex answered without a sign-in link. Try again in a moment.",
        color: "red",
      });
      return;
    }

    setPin(pin);

    const { width, height, features } = PLEX_AUTH_CONFIG.AUTH_WINDOW_CONFIG;
    const left = Math.round(window.screen.width / 2 - width / 2);
    const top = Math.round(window.screen.height / 2 - height / 2);

    authWindowRef.current = window.open(
      pin.authUrl,
      "PlexAuth",
      `width=${width},height=${height},left=${left},top=${top},${features}`,
    );

    // A blocked popup returns null, which otherwise leaves the panel polling a
    // pin against a window the user never saw.
    if (!authWindowRef.current) {
      setPin(null);
      notifications.show({
        title: "Plex sign-in window was blocked",
        message:
          "Allow pop-ups for this site, then start the Plex sign-in again.",
        color: "red",
      });
    }
  };

  // Signing out also turns use_plex off, which silences every Plex server,
  // including the ones added by hand, so it asks first, as the account card's
  // Disconnect does.
  const handleLogout = () =>
    modals.openConfirmModal({
      title: "Disconnect from Plex",
      children: (
        <Text size="sm">
          This signs you out of Plex and turns off Plex integration, which also
          stops refreshes to any Plex server you added by hand.
        </Text>
      ),
      labels: { confirm: "Disconnect", cancel: "Cancel" },
      confirmProps: { color: "red" },
      onConfirm: () =>
        logout(undefined, {
          onSuccess: () => {
            notifications.show({
              title: "Disconnected from Plex",
              message: "All settings related to Plex were removed",
              color: "green",
            });
          },
          // A refused sign-out had no handler here: the dialog closed, the
          // panel still said Connected, and all that could appear was the
          // client's bare "Error <code>". It says what happened instead.
          onError: (error) => {
            notifications.show({
              title: "Could not disconnect from Plex",
              message: `${
                reasonOf(error) ?? "Bazarr+ did not answer."
              } You are still signed in to Plex. Try again in a moment.`,
              color: "red",
            });
          },
        }),
    });

  const handleCancelAuth = () => {
    setPin(null);
    if (authWindowRef.current) {
      authWindowRef.current.close();
      authWindowRef.current = null;
    }
    // Refetch auth status when auth is cancelled
    void refetchAuth();
  };

  if (authIsLoading && !isPolling) {
    return <Text>Loading authentication status...</Text>;
  }

  if (isPolling && !pinData?.authenticated) {
    return (
      <Paper withBorder p="lg" className={styles.authSection}>
        <Stack gap="md">
          <Title order={4}>Plex account</Title>
          <Stack gap="sm">
            <Text size="lg" fw={600}>
              Finish signing in to Plex
            </Text>
            <Text>
              Sign-in code:{" "}
              <Text component="span" fw={700}>
                {pin?.code}
              </Text>
            </Text>
            <Text size="sm">Sign in to Plex in the window that opened.</Text>
            <Button
              onClick={handleCancelAuth}
              variant="light"
              color="gray"
              size="sm"
              className={styles.actionButton}
            >
              Cancel
            </Button>
          </Stack>
        </Stack>
      </Paper>
    );
  }

  if (!isAuthenticated) {
    return (
      <Paper withBorder p="lg" className={styles.authSection}>
        <Stack gap="md">
          <Title order={4}>Plex account</Title>
          <Stack gap="sm">
            <Text size="sm">
              Connect your Plex account to enable secure, automated integration
              with Bazarr.
            </Text>
            <Text size="xs" c="var(--bz-text-tertiary)">
              Advanced users: manual configuration is available in config.yaml
              if signing in here does not suit you.
            </Text>
            {authError && (
              <Alert color="red" variant="light">
                {authError.message || "Authentication failed"}
              </Alert>
            )}
            <Button
              onClick={handleAuth}
              variant="filled"
              color="brand"
              size="md"
              className={styles.actionButton}
            >
              Connect to Plex
            </Button>
          </Stack>
        </Stack>
      </Paper>
    );
  }

  // Authenticated state
  return (
    <Paper withBorder p="lg" className={styles.authSection}>
      <Stack gap="md">
        <Title order={4}>Plex account</Title>
        <Alert color="brand" variant="light" className={styles.authAlert}>
          Connected as {authData?.username} ({authData?.email})
        </Alert>
        <Button
          onClick={handleLogout}
          variant="light"
          color="gray"
          size="sm"
          className={styles.actionButton}
          loading={isLoggingOut}
          disabled={isLoggingOut}
        >
          Disconnect from Plex
        </Button>
      </Stack>
    </Paper>
  );
};

export default AuthSection;
