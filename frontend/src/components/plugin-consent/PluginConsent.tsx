import { FunctionComponent, useRef, useState } from "react";
import { Alert, Button, Group, Modal, Skeleton, Stack } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import {
  CONSENT,
  usePluginApplyUpdate,
  usePluginInstall,
  usePluginInstallation,
  usePluginInstallConsent,
  usePluginUpdateConsent,
} from "@/apis/hooks/plugins";
import { QueryKeys } from "@/apis/queries/keys";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import { InstallSummary, PluginCostCaps } from "./InstallSummary";
import { consentChanged, needsPassword, reasonOf } from "./refusal";
import { installedFacts, UpdateDelta } from "./UpdateDelta";
import styles from "./PluginConsent.module.scss";

/**
 * What the dialog is asked about: one plugin and one action. An install names the catalog entry it
 * would install. An update names only the plugin, because the server knows which release it moves to.
 */
export type PluginConsentTarget =
  | {
      action: "install";
      pluginId: string;
      name?: string;
      source: string;
      version: string;
    }
  | { action: "update"; pluginId: string; name?: string };

export interface PluginConsentProps {
  /** What is being asked, or null while nothing is. */
  target: PluginConsentTarget | null;
  onClose: () => void;
  /** Called once the install or the update has finished, before the dialog closes. */
  onDone?: (target: PluginConsentTarget) => void;
  /** The spending caps to show for an AI engine. Left out, no spending line is shown. */
  costCaps?: PluginCostCaps;
}

const FAILED = {
  install:
    "The install did not finish. Check the Activity tab of the Plugin Hub for the reason.",
  update:
    "The update did not finish. Check the Activity tab of the Plugin Hub for the reason.",
} as const;

/**
 * The consent dialog for an install or an update. It shows what the server says the action adds and
 * sends the digest of exactly that set back with the request, so a set that changed in between is
 * refused rather than installed unseen. Nothing is sent before the person confirms.
 *
 * It needs no page around it: the Plugin Hub mounts it, and so can any page that offers an install.
 */
const PluginConsent: FunctionComponent<PluginConsentProps> = ({
  target,
  onClose,
  onDone,
  costCaps,
}) => {
  const client = useQueryClient();
  const runGated = useGatedAction();
  const install = usePluginInstall({ inlineErrors: true });
  const applyUpdate = usePluginApplyUpdate({ inlineErrors: true });
  // The dialog keeps showing what it was asked about while it fades out.
  const shown = useRef(target);
  if (target) {
    shown.current = target;
  }
  const current = target ?? shown.current;
  const installing = target?.action === "install" ? target : null;
  const updating = target?.action === "update" ? target : null;
  const summaryQuery = usePluginInstallConsent(
    installing
      ? {
          source: installing.source,
          id: installing.pluginId,
          version: installing.version,
        }
      : null,
  );
  const deltaQuery = usePluginUpdateConsent(updating?.pluginId ?? null);
  const installedQuery = usePluginInstallation(updating?.pluginId ?? null);
  const query = installing ? summaryQuery : deltaQuery;

  const [problem, setProblem] = useState("");
  // One flag for the whole run: the password question and the job. Until it is over the buttons stay
  // off, so a second request cannot start on top.
  const [busy, setBusy] = useState(false);
  const running = useRef(false);

  const close = () => {
    setProblem("");
    onClose();
  };

  const digest = installing
    ? summaryQuery.data?.digest
    : deltaQuery.data?.digest;
  const ready = digest !== undefined && !query.isFetching;

  const run = async () => {
    if (!target || !digest || running.current) {
      return;
    }
    running.current = true;
    setBusy(true);
    setProblem("");
    const send = async () => {
      if (target.action === "install") {
        await install.mutateAsync({
          source: target.source,
          id: target.pluginId,
          version: target.version,
          consent: digest,
        });
      } else {
        await applyUpdate.mutateAsync({ id: target.pluginId, consent: digest });
      }
    };
    const action = `${target.action} ${target.name ?? target.pluginId}`;
    try {
      // Installing runs code on this machine, so the server asks for the password first where a login
      // is set up. The gate asks before the request goes out.
      let confirmed: boolean;
      try {
        confirmed = await runGated(send, action);
      } catch (error) {
        if (!needsPassword(error)) {
          throw error;
        }
        // The server wanted a confirmation the page believed it still had: the gate asks now, and the
        // request goes out once more behind the answer.
        confirmed = await runGated(send, action);
      }
      if (!confirmed) {
        return;
      }
      onDone?.(target);
      close();
    } catch (error) {
      if (consentChanged(error)) {
        // What the action adds is not what was shown. Show it again, and let the person decide again.
        setProblem(
          reasonOf(error) ||
            "What this would add has changed since it was shown to you. Review it again before you agree.",
        );
        void client.invalidateQueries({
          queryKey: [QueryKeys.Plugins, CONSENT],
        });
        return;
      }
      setProblem(reasonOf(error) || FAILED[target.action]);
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const name = current?.name ?? current?.pluginId ?? "";
  const title = current
    ? `${current.action === "install" ? "Install" : "Update"} ${name}?`
    : "";
  const verb = current?.action === "update" ? "Update" : "Install";

  return (
    <Modal
      opened={target !== null}
      onClose={busy ? () => undefined : close}
      withCloseButton={!busy}
      closeButtonProps={{ "aria-label": "Close" }}
      closeOnEscape={!busy}
      closeOnClickOutside={!busy}
      centered
      size="lg"
      // Focus starts on the heading, not on a button, so nothing is one keypress from agreeing.
      title={
        <span className={styles.heading} tabIndex={-1} data-autofocus>
          {title}
        </span>
      }
    >
      {target && (
        <Stack gap="md">
          {query.isPending ? (
            <div
              aria-busy="true"
              aria-label="Loading what this would add"
              className={styles.body}
            >
              <Skeleton height={14} width="60%" />
              <Skeleton height={14} />
              <Skeleton height={14} width="80%" />
            </div>
          ) : query.isError ? (
            <Alert color="red" role="alert">
              {reasonOf(query.error) ||
                "What this would add could not be loaded, so it cannot be agreed to yet."}
            </Alert>
          ) : (
            <div className={styles.body}>
              {installing && summaryQuery.data && (
                <InstallSummary
                  summary={summaryQuery.data}
                  costCaps={costCaps}
                />
              )}
              {updating && deltaQuery.data && (
                <UpdateDelta
                  delta={deltaQuery.data}
                  before={installedFacts(installedQuery.data)}
                />
              )}
            </div>
          )}
          {problem && (
            <Alert color="red" role="alert">
              {problem}
            </Alert>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={close} disabled={busy}>
              Cancel
            </Button>
            {query.isError ? (
              <Button
                variant="default"
                onClick={() => void query.refetch()}
                loading={query.isFetching}
              >
                Try again
              </Button>
            ) : (
              <Button
                onClick={() => void run()}
                loading={busy}
                disabled={!ready}
              >
                {verb}
              </Button>
            )}
          </Group>
        </Stack>
      )}
    </Modal>
  );
};

export default PluginConsent;
