import { FunctionComponent, useRef, useState } from "react";
import { Alert, Button, Group, Modal, Skeleton, Stack } from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import {
  CONSENT,
  usePluginApplyUpdate,
  usePluginInstall,
  usePluginInstallation,
  usePluginInstallConsent,
  usePluginRollback,
  usePluginRollbackPreview,
  usePluginUpdateConsent,
} from "@/apis/hooks/plugins";
import { QueryKeys } from "@/apis/queries/keys";
import type { PluginRollbackResult } from "@/apis/raw/plugins";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import { notification } from "@/modules/notification";
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
      /** The kind of the plugin, when the caller knows it. A subtitle provider whose closure adds no other kind installs without the dialog. */
      kind?: string;
      /**
       * How the action is worded to the person. A new release of an installed plugin is sent as an install
       * of that release, and a caller that does so says "update", so every prompt and failure does too.
       */
      wording?: "install" | "update";
    }
  | { action: "update"; pluginId: string; name?: string }
  | {
      action: "rollback";
      pluginId: string;
      name?: string;
      /** The previous version the card offered. The fresh preview may change it. */
      version: string;
    };

export interface PluginConsentProps {
  /** What is being asked, or null while nothing is. */
  target: PluginConsentTarget | null;
  onClose: () => void;
  /** Called once the install or the update has finished, before the dialog closes. */
  onDone?: (target: PluginConsentTarget) => void;
  /** Called when the dialog has finished closing, for a page that has to put focus somewhere itself. */
  onClosed?: () => void;
  /** The spending caps to show for an AI engine. Left out, no spending line is shown. */
  costCaps?: PluginCostCaps;
}

/** How a target is worded to the person: its own action, unless the caller said otherwise. */
export function wordingOf(
  target: PluginConsentTarget,
): "install" | "update" | "roll back" {
  return target.action === "install"
    ? (target.wording ?? "install")
    : target.action === "rollback"
      ? "roll back"
      : "update";
}

const FAILED = {
  install:
    "The install did not finish. Check the Activity tab of the Plugin Hub for the reason.",
  update:
    "The update did not finish. Check the Activity tab of the Plugin Hub for the reason.",
  "roll back":
    "The roll back did not finish. Try again or check the plugin log.",
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
  onClosed,
  costCaps,
}) => {
  const client = useQueryClient();
  const runGated = useGatedAction();
  const install = usePluginInstall({ inlineErrors: true });
  const applyUpdate = usePluginApplyUpdate({ inlineErrors: true });
  const rollback = usePluginRollback();
  // The dialog keeps showing what it was asked about while it fades out.
  const shown = useRef(target);
  if (target) {
    shown.current = target;
  }
  const current = target ?? shown.current;
  const installing = target?.action === "install" ? target : null;
  const updating = target?.action === "update" ? target : null;
  const rollingBack = target?.action === "rollback" ? target : null;
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
  const rollbackQuery = usePluginRollbackPreview(rollingBack?.pluginId ?? null);
  const installedQuery = usePluginInstallation(
    updating?.pluginId ?? rollingBack?.pluginId ?? null,
  );
  const query = installing
    ? summaryQuery
    : rollingBack
      ? rollbackQuery
      : deltaQuery;

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
    : rollingBack
      ? rollbackQuery.data?.consent?.digest
      : deltaQuery.data?.digest;
  // What the person confirms is what is on screen: the set is shown only once everything it is drawn
  // from has arrived, and the buttons that agree to it wait for the same moment.
  const complete =
    !query.isPending &&
    !((updating || rollingBack) && installedQuery.isPending) &&
    !(rollingBack && installedQuery.isFetching);
  const rollbackPreview = rollbackQuery.data;
  const ready =
    !query.isError &&
    !query.isFetching &&
    complete &&
    (rollingBack
      ? Boolean(
          rollbackPreview?.previous_version &&
          !rollbackPreview.blocked_reason &&
          (!rollbackPreview.consent ||
            (digest &&
              rollbackPreview.consent.plugin.to_version ===
                rollbackPreview.previous_version)),
        )
      : Boolean(digest));

  const run = async () => {
    if (!target || !ready || running.current) {
      return;
    }
    running.current = true;
    setBusy(true);
    setProblem("");
    let rollbackResult: PluginRollbackResult | undefined;
    const send = async () => {
      if (target.action === "install") {
        await install.mutateAsync({
          source: target.source,
          id: target.pluginId,
          version: target.version,
          consent: digest,
        });
      } else if (target.action === "rollback") {
        rollbackResult = await rollback.mutateAsync({
          id: target.pluginId,
          version: rollbackPreview!.previous_version!,
          ...(digest ? { consent: digest } : {}),
        });
      } else {
        await applyUpdate.mutateAsync({ id: target.pluginId, consent: digest });
      }
    };
    const action = `${wordingOf(target)} ${target.name ?? target.pluginId}`;
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
      if (rollbackResult) {
        const name = target.name ?? target.pluginId;
        showNotification(
          notification.info(
            rollbackResult.status === "activated"
              ? "Rolled back"
              : "Roll back awaiting restart",
            rollbackResult.status === "activated"
              ? `${name} v${rollbackResult.version} is now active.`
              : `${name} v${rollbackResult.version} is staged. Restart Bazarr+ when you are ready to activate it.`,
          ),
        );
      }
      onDone?.(target);
      close();
    } catch (error) {
      // A refusal can come twice. An install is refused at the request with a 409. An update is
      // accepted and then refused by its job, and an install is checked again when its job starts.
      // Either way what was shown may no longer be what the action would add, so the set and its
      // digest are read again, and the person confirms what is shown next.
      void client.invalidateQueries({
        queryKey: [QueryKeys.Plugins, CONSENT],
      });
      if (consentChanged(error)) {
        setProblem(
          reasonOf(error) ||
            (rollingBack
              ? "The previous release or its access has changed. Review it again before confirming."
              : "What this would add has changed since it was shown to you. Review it again before you agree."),
        );
        return;
      }
      setProblem(reasonOf(error) || FAILED[wordingOf(target)]);
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const name = current?.name ?? current?.pluginId ?? "";
  const verb =
    current?.action === "rollback"
      ? "Roll back"
      : current && wordingOf(current) === "update"
        ? "Update"
        : "Install";
  const title = current ? `${verb} ${name}?` : "";

  return (
    <Modal
      opened={target !== null}
      onClose={busy ? () => undefined : close}
      withCloseButton={!busy}
      closeButtonProps={{ "aria-label": "Close" }}
      onExitTransitionEnd={onClosed}
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
          {!complete ? (
            <div
              role="status"
              aria-busy="true"
              aria-label={
                rollingBack
                  ? "Loading the previous release"
                  : "Loading what this would add"
              }
              className={styles.body}
            >
              <Skeleton height={14} width="60%" />
              <Skeleton height={14} />
              <Skeleton height={14} width="80%" />
            </div>
          ) : query.isError ? (
            <Alert color="red" role="alert">
              {reasonOf(query.error) ||
                (rollingBack
                  ? "The previous release could not be loaded, so this roll back cannot be confirmed yet."
                  : "What this would add could not be loaded, so it cannot be agreed to yet.")}
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
              {rollingBack && rollbackPreview && (
                <>
                  {rollbackPreview.previous_version && (
                    <p>
                      Previous release: v{rollbackPreview.previous_version}.
                    </p>
                  )}
                  {rollbackPreview.previous_version &&
                    rollbackPreview.previous_version !==
                      rollingBack.version && (
                      <Alert color="yellow" role="alert">
                        The previous release changed from v{rollingBack.version}{" "}
                        to v{rollbackPreview.previous_version}. Review this
                        release before confirming.
                      </Alert>
                    )}
                  {rollbackPreview.blocked_reason ||
                  !rollbackPreview.previous_version ? (
                    <Alert color="red" role="alert">
                      {rollbackPreview.blocked_reason ||
                        "No previous release is available."}
                    </Alert>
                  ) : (
                    <>
                      <p>
                        This returns the plugin code to the previous release and
                        uses your current plugin data and settings, including
                        changes made since the update. No backup of data or
                        settings is restored. Activation may remove settings
                        this release does not support or turn incompatible saved
                        instances off for review.
                      </p>
                      <p className={styles.quiet}>
                        The release becomes active now if this plugin supports
                        it. Otherwise it waits until you restart Bazarr+.
                      </p>
                      {rollbackPreview.consent ? (
                        <UpdateDelta
                          delta={rollbackPreview.consent}
                          before={installedFacts(installedQuery.data)}
                          action="roll back"
                        />
                      ) : (
                        <p className={styles.quiet}>
                          This roll back adds no new destination, permission,
                          program or plugin.
                        </p>
                      )}
                    </>
                  )}
                </>
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
