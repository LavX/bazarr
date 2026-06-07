import {
  FunctionComponent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Outlet, useNavigate } from "react-router";
import {
  Alert,
  AppShell,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  Stack,
  Text,
} from "@mantine/core";
import { useWindowEvent } from "@mantine/hooks";
import { showNotification } from "@mantine/notifications";
import {
  faCheck,
  faCircle,
  faExclamationTriangle,
  faSpinner,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQueryClient } from "@tanstack/react-query";
import { useSystemSettings } from "@/apis/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import AppNavbar from "@/App/Navbar";
import logoSrc from "@/assets/images/logo_no_orb128.png";
import ErrorBoundary from "@/components/ErrorBoundary";
import { useWhatsNewAutoOpen } from "@/components/modals/useWhatsNewAutoOpen";
import NavbarProvider from "@/contexts/Navbar";
import OnlineProvider from "@/contexts/Online";
import { notification } from "@/modules/task";
import CriticalError from "@/pages/errors/CriticalError";
import { RouterNames } from "@/Router/RouterNames";
import { Environment } from "@/utilities";
import { consumeRestartReloadPending } from "@/utilities/restart";
import { registerAppNavigate } from "@/utilities/whatsNew";
import AppHeader from "./Header";
import styleVars from "@/assets/_variables.module.scss";

interface SupervisorStatus {
  state: "starting" | "running" | "crashed" | "stopping";
  stage: string;
  stage_index: number;
  stage_total: number;
  stages: string[];
}

const App: FunctionComponent = () => {
  const navigate = useNavigate();

  // Expose a live navigate to the What's New modal (it renders outside the Router, and the
  // router is rebuilt on data changes, so a captured navigate would go stale).
  useEffect(() => {
    registerAppNavigate(navigate);
  }, [navigate]);

  const [criticalError, setCriticalError] = useState<string | null>(null);
  const [navbar, setNavbar] = useState(false);
  const [online, setOnline] = useState(false);
  const [hasConnected, setHasConnected] = useState(false);
  const [supervisor, setSupervisor] = useState<SupervisorStatus | null>(null);
  const previousOnline = useRef(false);

  const queryClient = useQueryClient();
  const settings = useSystemSettings();
  const settingsLoaded = settings.data !== undefined;

  // Show the "What's New" wizard once after upgrading, but only once authenticated and
  // loaded (settingsLoaded) so it never appears over the login screen.
  useWhatsNewAutoOpen(settingsLoaded);

  // Stream supervisor status via SSE during startup.
  // When backend reports "running", invalidate the settings cache to trigger
  // an immediate refetch (bypasses any retry/backoff state).
  useEffect(() => {
    if (settingsLoaded || typeof EventSource === "undefined") return;

    const url = `${Environment.baseUrl}/_supervisor/events`;
    const es = new EventSource(url);
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setSupervisor(data);
        if (data.state === "running") {
          void queryClient.invalidateQueries({
            queryKey: [QueryKeys.System, QueryKeys.Settings],
          });
        }
      } catch {
        // ignore parse errors
      }
    };
    return () => es.close();
  }, [settingsLoaded, queryClient]);

  useWindowEvent("app-critical-error", ({ detail }) => {
    setCriticalError(detail.message);
  });

  useWindowEvent("app-auth-changed", (ev) => {
    if (!ev.detail.authenticated) {
      navigate(RouterNames.Auth);
    }
  });

  useWindowEvent("app-online-status", ({ detail }) => {
    setOnline(detail.online);
    if (detail.online) {
      setHasConnected(true);
    }
  });

  useEffect(() => {
    const reconnected = online && hasConnected && !previousOnline.current;
    previousOnline.current = online;

    if (!reconnected) {
      return;
    }

    void queryClient.invalidateQueries();
    void queryClient.refetchQueries({ type: "active" });

    if (consumeRestartReloadPending()) {
      window.setTimeout(() => {
        window.location.reload();
      }, 250);
    }
  }, [hasConnected, online, queryClient]);

  const [upgradeModalOpen, setUpgradeModalOpen] = useState(false);
  const [upgrading, setUpgrading] = useState(false);

  useEffect(() => {
    if (Environment.hasUpdate) {
      showNotification(
        notification.info(
          "Update available",
          "A new version of Bazarr is ready, restart is required",
        ),
      );
    }
  }, []);

  useEffect(() => {
    const token = sessionStorage.getItem("password_upgrade_token");
    if (token) {
      setUpgradeModalOpen(true);
    }
  }, []);

  const handleUpgradeAccept = useCallback(async () => {
    const token = sessionStorage.getItem("password_upgrade_token");
    if (!token) return;
    setUpgrading(true);
    try {
      await api.system.upgradePasswordHash(token);
      showNotification(
        notification.info(
          "Password upgraded",
          "Your password hash has been upgraded to PBKDF2-SHA256",
        ),
      );
    } catch {
      showNotification(
        notification.warn("Upgrade failed", "Could not upgrade password hash"),
      );
    } finally {
      sessionStorage.removeItem("password_upgrade_token");
      setUpgradeModalOpen(false);
      setUpgrading(false);
    }
  }, []);

  const handleUpgradeDecline = useCallback(() => {
    sessionStorage.removeItem("password_upgrade_token");
    setUpgradeModalOpen(false);
  }, []);

  if (criticalError !== null) {
    return <CriticalError message={criticalError}></CriticalError>;
  }

  // Startup screen: settings not loaded yet (backend not ready)
  if (!settingsLoaded) {
    const isCrashed = supervisor?.state === "crashed";
    const stages = supervisor?.stages ?? [];
    const stageIndex = supervisor?.stage_index ?? -1;
    // Add "Loading configuration" as final frontend-only stage
    const allStages =
      stages.length > 0
        ? [...stages.slice(0, -1), "Loading configuration"]
        : [];
    const currentIndex =
      supervisor?.state === "running" && !settingsLoaded
        ? allStages.length - 1
        : stageIndex;

    return (
      <Center
        style={{
          height: "100dvh",
          background: "var(--bz-surface-ground, #121125)",
        }}
      >
        <Stack align="center" gap="lg">
          <img
            src={logoSrc}
            alt="Bazarr+"
            width={64}
            height={64}
            style={{ opacity: 0.8 }}
          />
          <Text size="lg" fw={600} c="var(--bz-text-primary)">
            Bazarr+ is starting up
          </Text>
          {isCrashed ? (
            <Text size="sm" c="red">
              Backend process crashed. Restarting...
            </Text>
          ) : (
            <Stack gap={4} style={{ minWidth: 220 }}>
              {allStages.map((stage, i) => {
                const done = i < currentIndex;
                const active = i === currentIndex;
                return (
                  <Group key={stage} gap="xs" wrap="nowrap">
                    <FontAwesomeIcon
                      icon={done ? faCheck : active ? faSpinner : faCircle}
                      size="xs"
                      spin={active}
                      style={{
                        width: 14,
                        color: done
                          ? "var(--mantine-color-green-6)"
                          : active
                            ? "var(--mantine-color-orange-5)"
                            : "var(--bz-text-disabled, #555)",
                      }}
                    />
                    <Text
                      size="xs"
                      c={
                        done
                          ? "var(--bz-text-tertiary)"
                          : active
                            ? "var(--bz-text-primary)"
                            : "var(--bz-text-disabled)"
                      }
                      fw={active ? 500 : 400}
                    >
                      {stage}
                    </Text>
                  </Group>
                );
              })}
            </Stack>
          )}
          <Loader size="sm" color={isCrashed ? "red" : "orange"} />
        </Stack>
      </Center>
    );
  }

  return (
    <ErrorBoundary>
      <NavbarProvider value={{ showed: navbar, show: setNavbar }}>
        <OnlineProvider value={{ online, setOnline }}>
          <AppShell
            navbar={{
              width: styleVars.navBarWidth,
              breakpoint: "sm",
              collapsed: { mobile: !navbar },
            }}
            header={{ height: { base: styleVars.headerHeight } }}
            padding={0}
          >
            <AppHeader></AppHeader>
            <AppNavbar></AppNavbar>
            <AppShell.Main>
              {!online && hasConnected && (
                <Alert
                  color="yellow"
                  icon={<FontAwesomeIcon icon={faExclamationTriangle} />}
                  mx="md"
                  mt="md"
                  radius="md"
                >
                  <Text size="sm">
                    Connection to backend lost. Reconnecting...
                  </Text>
                </Alert>
              )}
              <Outlet></Outlet>
            </AppShell.Main>
          </AppShell>
          <Modal
            opened={upgradeModalOpen}
            onClose={handleUpgradeDecline}
            title="Upgrade Password Security"
            centered
          >
            <Stack>
              <Text size="sm">
                Your password is currently stored using a weak MD5 hash. Would
                you like to upgrade to PBKDF2-SHA256 for better security?
              </Text>
              <Text size="xs" c="var(--bz-text-tertiary)">
                Note: After upgrading, reverting to upstream Bazarr will require
                resetting your password via the config file.
              </Text>
              <Group justify="flex-end">
                <Button variant="default" onClick={handleUpgradeDecline}>
                  Not now
                </Button>
                <Button onClick={handleUpgradeAccept} loading={upgrading}>
                  Upgrade
                </Button>
              </Group>
            </Stack>
          </Modal>
        </OnlineProvider>
      </NavbarProvider>
    </ErrorBoundary>
  );
};

export default App;
