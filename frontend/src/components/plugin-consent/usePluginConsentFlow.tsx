import { ReactElement, useCallback, useRef, useState } from "react";
import { showNotification } from "@mantine/notifications";
import { usePluginApplyUpdate } from "@/apis/hooks/plugins";
import api from "@/apis/raw";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import { notification } from "@/modules/notification";
import { PluginCostCaps } from "./InstallSummary";
import PluginConsent, { PluginConsentTarget } from "./PluginConsent";
import { needsPassword, reasonOf } from "./refusal";

export interface PluginConsentFlow {
  /**
   * Start an install or an update. An install always opens the dialog. An update opens it only when
   * the server says it adds a destination, a permission, a program or a plugin, and otherwise goes
   * ahead on its own, with the digest the server gave.
   */
  begin: (target: PluginConsentTarget) => Promise<void>;
  /** The dialog. Render it once, anywhere under the providers. */
  dialog: ReactElement;
  /** The plugin whose update is going ahead without the dialog, if one is. */
  updating: string | null;
}

/**
 * The decision "does this one need the dialog" in one place, for every page that offers an install or
 * an update. The dialog it renders is the same component that works mounted on its own.
 */
export function usePluginConsentFlow({
  onDone,
  costCaps,
}: {
  onDone?: (target: PluginConsentTarget) => void;
  costCaps?: PluginCostCaps;
} = {}): PluginConsentFlow {
  const [target, setTarget] = useState<PluginConsentTarget | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const running = useRef(false);
  const runGated = useGatedAction();
  const applyUpdate = usePluginApplyUpdate({ inlineErrors: true });

  const begin = useCallback(
    async (next: PluginConsentTarget) => {
      if (next.action === "install") {
        setTarget(next);
        return;
      }
      if (running.current) {
        return;
      }
      running.current = true;
      setUpdating(next.pluginId);
      const label = next.name ?? next.pluginId;
      try {
        const delta = await api.plugins.updateConsent(next.pluginId);
        if (delta.needs_consent) {
          setTarget(next);
          return;
        }
        const send = () =>
          applyUpdate.mutateAsync({ id: next.pluginId, consent: delta.digest });
        let confirmed: boolean;
        try {
          confirmed = await runGated(send, `update ${label}`);
        } catch (error) {
          if (!needsPassword(error)) {
            throw error;
          }
          confirmed = await runGated(send, `update ${label}`);
        }
        if (confirmed) {
          onDone?.(next);
        }
      } catch (error) {
        showNotification(
          notification.error(
            `${label} was not updated`,
            reasonOf(error) ||
              "The update did not finish. Check the Activity tab for the reason.",
          ),
        );
      } finally {
        running.current = false;
        setUpdating(null);
      }
    },
    [applyUpdate, onDone, runGated],
  );

  const dialog = (
    <PluginConsent
      target={target}
      costCaps={costCaps}
      onClose={() => setTarget(null)}
      onDone={onDone}
    />
  );
  return { begin, dialog, updating };
}
