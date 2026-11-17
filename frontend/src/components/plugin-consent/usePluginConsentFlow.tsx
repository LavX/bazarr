import { ReactElement, useCallback, useRef, useState } from "react";
import { showNotification } from "@mantine/notifications";
import { usePluginApplyUpdate, usePluginInstall } from "@/apis/hooks/plugins";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { SUBTITLE_PROVIDER_KIND } from "@/apis/raw/plugins";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import { notification } from "@/modules/notification";
import { PluginCostCaps } from "./InstallSummary";
import PluginConsent, { PluginConsentTarget } from "./PluginConsent";
import { needsPassword, reasonOf } from "./refusal";

export interface PluginConsentFlow {
  /**
   * Start an install or an update. The dialog opens for an install, except for a subtitle provider
   * whose closure adds no plugin of another kind, which installs as it always has. An update opens it
   * only when the server says it adds a destination, a permission, a program or a plugin, and
   * otherwise goes ahead on its own, with the digest the server gave.
   */
  begin: (target: PluginConsentTarget) => Promise<void>;
  /** The dialog. Render it once, anywhere under the providers. */
  dialog: ReactElement;
  /** The plugin whose install or update is going ahead without the dialog, if one is. */
  updating: string | null;
}

/**
 * The decision "does this one need the dialog" in one place, for every page that offers an install or
 * an update. The dialog it renders is the same component that works mounted on its own.
 */
export function usePluginConsentFlow({
  onDone,
  onClosed,
  costCaps,
}: {
  onDone?: (target: PluginConsentTarget) => void;
  onClosed?: () => void;
  costCaps?: PluginCostCaps;
} = {}): PluginConsentFlow {
  const [target, setTarget] = useState<PluginConsentTarget | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const running = useRef(false);
  const runGated = useGatedAction();
  const applyUpdate = usePluginApplyUpdate({ inlineErrors: true });
  const install = usePluginInstall({ inlineErrors: true });

  /** One request behind the password gate, asked again once if the server wanted a fresh confirmation. */
  const gated = useCallback(
    async (send: () => Promise<unknown>, label: string) => {
      try {
        return await runGated(async () => void (await send()), label);
      } catch (error) {
        if (!needsPassword(error)) {
          throw error;
        }
        return runGated(async () => void (await send()), label);
      }
    },
    [runGated],
  );

  const begin = useCallback(
    async (next: PluginConsentTarget) => {
      if (running.current) {
        return;
      }
      running.current = true;
      setUpdating(next.pluginId);
      const label = next.name ?? next.pluginId;
      try {
        if (next.action === "install") {
          if (next.kind === SUBTITLE_PROVIDER_KIND) {
            const summary = await withInlineErrors(() =>
              api.plugins.installConsent(
                next.source,
                next.pluginId,
                next.version,
              ),
            ).catch(() => null);
            if (
              summary &&
              summary.members.every(
                (member) => member.kind === SUBTITLE_PROVIDER_KIND,
              )
            ) {
              const confirmed = await gated(
                () =>
                  install.mutateAsync({
                    source: next.source,
                    id: next.pluginId,
                    version: next.version,
                    consent: summary.digest,
                  }),
                `install ${label}`,
              );
              if (confirmed) {
                onDone?.(next);
              }
              return;
            }
          }
          setTarget(next);
          return;
        }
        const delta = await withInlineErrors(() =>
          api.plugins.updateConsent(next.pluginId),
        );
        if (delta.needs_consent) {
          setTarget(next);
          return;
        }
        const confirmed = await gated(
          () =>
            applyUpdate.mutateAsync({
              id: next.pluginId,
              consent: delta.digest,
            }),
          `update ${label}`,
        );
        if (confirmed) {
          onDone?.(next);
        }
      } catch (error) {
        showNotification(
          notification.error(
            `${label} was not ${next.action === "install" ? "installed" : "updated"}`,
            reasonOf(error) ||
              `The ${next.action} did not finish. Check the Activity tab for the reason.`,
          ),
        );
      } finally {
        running.current = false;
        setUpdating(null);
      }
    },
    [applyUpdate, gated, install, onDone],
  );

  const dialog = (
    <PluginConsent
      target={target}
      costCaps={costCaps}
      onClose={() => setTarget(null)}
      onClosed={onClosed}
      onDone={onDone}
    />
  );
  return { begin, dialog, updating };
}
