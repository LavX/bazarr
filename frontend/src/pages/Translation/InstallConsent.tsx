/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useRef, useState } from "react";
import {
  Alert,
  Button,
  Code,
  Group,
  List,
  Modal,
  Stack,
  Text,
} from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useProviderHubInstall } from "@/apis/hooks/providerHub";
import { useFreshState } from "@/apis/hooks/translation";
import { QueryKeys } from "@/apis/queries/keys";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import type { TranslationAvailableEngine } from "@/types/translation";

/** The longest text body that is taken for a sentence the server wrote for a person. */
const SENTENCE_LIMIT = 300;

/**
 * The server's own words in a refusal: the message of a body that is an object, or a body that is
 * plain text. An error page from a proxy is text too, so a body that is markup, or too long to be a
 * sentence, is not shown: the page says what failed in its own words instead.
 */
function refusalText(body: unknown): string {
  if (typeof body === "string") {
    const text = body.trim();
    return text.startsWith("<") || text.length >= SENTENCE_LIMIT ? "" : text;
  }
  if (typeof body === "object" && body !== null) {
    const message = (body as { message?: unknown }).message;
    return typeof message === "string" ? message.trim() : "";
  }
  return "";
}

/** Whether the server refused because it wants the password confirmed first. */
const needsPassword = (error: unknown) =>
  isAxiosError(error) &&
  error.response?.status === 403 &&
  (error.response.data as { code?: string } | undefined)?.code ===
    "confirm_password";

export interface InstallConsentProps {
  /** The engine to install, or null while nothing is being asked. */
  engine: TranslationAvailableEngine | null;
  onClose: () => void;
  /** Called once the plugin is installed and the page has its new state. */
  onInstalled?: (engine: TranslationAvailableEngine) => void;
}

/** What installing an engine means, asked once, before the install job starts. */
const InstallConsent: FunctionComponent<InstallConsentProps> = ({
  engine,
  onClose,
  onInstalled,
}) => {
  const client = useQueryClient();
  const install = useProviderHubInstall({ inlineErrors: true });
  const runGated = useGatedAction();
  const [problem, setProblem] = useState("");
  // One flag for the whole run: the password question, the install job and the load of the new state.
  // Until all of it is over the buttons stay off, so a second install cannot be started on top.
  const [busy, setBusy] = useState(false);
  const running = useRef(false);
  // The install itself went through. What can still fail is loading the page's new state.
  const installedNow = useRef(false);
  // That load failed: the question now offers the refresh, not the install.
  const [behind, setBehind] = useState(false);
  const fresh = useFreshState();
  const destinations = engine?.destinations ?? [];
  // A failure belongs to the attempt it came from: closing the question forgets it.
  const close = () => {
    setProblem("");
    if (installedNow.current) {
      // Closed before the page caught up: it is asked once more on the way out.
      void client.invalidateQueries({ queryKey: [QueryKeys.Translation] });
    }
    onClose();
  };

  // Loads the state the install changed. It rejects when the load fails, so a page that could not be
  // refreshed is never taken for one that was.
  const refresh = async () => {
    await fresh();
    void client.invalidateQueries({ queryKey: [QueryKeys.Translation] });
  };

  const run = async () => {
    if (!engine || running.current) {
      return;
    }
    running.current = true;
    setBusy(true);
    setProblem("");
    const installIt = async () => {
      await install.mutateAsync({
        source: engine.source,
        provider_id: engine.plugin_id,
        version: engine.version,
      });
      // From here on the plugin is installed, whatever happens to the page.
      installedNow.current = true;
      await refresh();
    };
    const action = `install ${engine.name}`;
    try {
      if (installedNow.current) {
        // Installed on an earlier press; only the page is behind.
        await refresh();
      } else {
        // Installing runs code on this machine, so the server asks for the password first where a
        // login is set up. The gate asks before the request goes out.
        let confirmed: boolean;
        try {
          confirmed = await runGated(installIt, action);
        } catch (error) {
          if (installedNow.current || !needsPassword(error)) {
            throw error;
          }
          // The server wanted a confirmation the page believed it still had. The remembered one is
          // gone now, so the gate asks, and the install goes out once more behind the answer.
          confirmed = await runGated(installIt, action);
        }
        if (!confirmed) {
          return;
        }
      }
      onInstalled?.(engine);
      close();
    } catch (error) {
      if (installedNow.current) {
        // Not a failed install: the page is what could not be loaded.
        setBehind(true);
        setProblem(
          `${engine.name} is installed, but the page could not be refreshed, so it does not show it yet.`,
        );
        return;
      }
      // A request that was refused says why in the server's words, when it has any. A job that
      // failed carries its own reason. Anything else gets the page's sentence, never the transport's.
      const reason = isAxiosError(error)
        ? refusalText(error.response?.data)
        : error instanceof Error
          ? error.message
          : "";
      setProblem(
        reason
          ? `The install did not finish: ${reason}`
          : "The install did not finish. Check the Subtitle Hub activity for the reason.",
      );
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  return (
    <Modal
      opened={engine !== null}
      onClose={busy ? () => undefined : close}
      withCloseButton={!busy}
      title={engine ? `Install ${engine.name}?` : ""}
      centered
    >
      {engine && (
        <Stack>
          <Text size="sm">
            Bazarr+ downloads {engine.name} {engine.version} from the plugin
            catalog and runs it as a plugin on this machine.
          </Text>
          {destinations.length > 0 && (
            <>
              <Text size="sm">
                When it translates, the subtitle text
                {engine.free ? "" : ", and your key,"} is sent to:
              </Text>
              <List size="sm" spacing={4}>
                {destinations.map((destination) => (
                  <List.Item key={destination}>
                    <Code>{destination}</Code>
                  </List.Item>
                ))}
              </List>
            </>
          )}
          <Text size="sm" c="dimmed">
            You can remove it at any time in the Subtitle Hub.
          </Text>
          {problem && (
            <Alert color="red" role="alert">
              {problem}
            </Alert>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={close} disabled={busy}>
              {behind ? "Close" : "Cancel"}
            </Button>
            <Button onClick={() => void run()} loading={busy}>
              {behind ? "Refresh the page" : "Install"}
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
};

export default InstallConsent;
