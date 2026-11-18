import { ReactElement, useCallback } from "react";
import { useTranslationSummary } from "@/apis/hooks/translation";
import {
  PluginCostCaps,
  usePluginConsentFlow,
} from "@/components/plugin-consent";
import { OFFICIAL_PLUGIN_ID } from "@/pages/PluginHub/model";
import type {
  TranslationAvailableEngine,
  TranslationState,
} from "@/types/translation";

export interface EngineInstall {
  /** Ask about installing an engine. Nothing is sent until the person agrees in the dialog. */
  begin: (engine: TranslationAvailableEngine) => void;
  /** The consent dialog. Render it once, next to the buttons that call `begin`. */
  dialog: ReactElement;
  /** The plugin whose install is going ahead, or null. Buttons that start another install wait on it. */
  installing: string | null;
}

/**
 * Installing an engine from the Translation page or the setup wizard: the same consent dialog the
 * Plugin Hub shows, with the caps this page already knows about, so an AI engine's install says what
 * it may spend. The request carries the digest of what the dialog showed, and a refusal is shown in
 * the dialog in the server's words.
 */
export function useEngineInstall(
  state: Pick<TranslationState, "capabilities"> | undefined,
): EngineInstall {
  const capabilities = state?.capabilities;
  const summary = useTranslationSummary(
    capabilities?.includes("summary") === true &&
      capabilities.includes("limits"),
  );
  const costCaps: PluginCostCaps | undefined = summary.data
    ? {
        jobCap: summary.data.job_cap,
        monthCap: summary.data.month.cap,
        pluginId: OFFICIAL_PLUGIN_ID,
      }
    : undefined;
  const flow = usePluginConsentFlow({ costCaps });
  const { begin: open } = flow;
  const begin = useCallback(
    (engine: TranslationAvailableEngine) => {
      void open({
        action: "install",
        pluginId: engine.plugin_id,
        name: engine.name,
        source: engine.source,
        version: engine.version,
      });
    },
    [open],
  );
  return { begin, dialog: flow.dialog, installing: flow.updating };
}
