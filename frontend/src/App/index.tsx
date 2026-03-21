import { FunctionComponent, useCallback, useEffect, useState } from "react";
import { Outlet, useNavigate } from "react-router";
import { AppShell, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { useWindowEvent } from "@mantine/hooks";
import { showNotification } from "@mantine/notifications";
import api from "@/apis/raw";
import AppNavbar from "@/App/Navbar";
import ErrorBoundary from "@/components/ErrorBoundary";
import NavbarProvider from "@/contexts/Navbar";
import OnlineProvider from "@/contexts/Online";
import { notification } from "@/modules/task";
import CriticalError from "@/pages/errors/CriticalError";
import { RouterNames } from "@/Router/RouterNames";
import { Environment } from "@/utilities";
import AppHeader from "./Header";
import styleVars from "@/assets/_variables.module.scss";

const App: FunctionComponent = () => {
  const navigate = useNavigate();

  const [criticalError, setCriticalError] = useState<string | null>(null);
  const [navbar, setNavbar] = useState(false);
  const [online, setOnline] = useState(true);

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
  });

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
    const pending = sessionStorage.getItem("password_upgrade_pending");
    if (pending) {
      setUpgradeModalOpen(true);
    }
  }, []);

  const handleUpgradeAccept = useCallback(async () => {
    const raw = sessionStorage.getItem("password_upgrade_pending");
    if (!raw) return;
    setUpgrading(true);
    try {
      const { username, password } = JSON.parse(raw);
      await api.system.upgradePasswordHash(username, password);
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
      sessionStorage.removeItem("password_upgrade_pending");
      setUpgradeModalOpen(false);
      setUpgrading(false);
    }
  }, []);

  const handleUpgradeDecline = useCallback(() => {
    sessionStorage.removeItem("password_upgrade_pending");
    setUpgradeModalOpen(false);
  }, []);

  if (criticalError !== null) {
    return <CriticalError message={criticalError}></CriticalError>;
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
                Your password is currently stored using a weak MD5 hash.
                Would you like to upgrade to PBKDF2-SHA256 for better security?
              </Text>
              <Text size="xs" c="dimmed">
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
