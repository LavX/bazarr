import { ReactElement, useCallback, useMemo } from "react";
import { usePluginInstallations } from "@/apis/hooks/plugins";
import { useTranslationSummary } from "@/apis/hooks/translation";
import {
  PluginCostCaps,
  usePluginConsentFlow,
} from "@/components/plugin-consent";
import {
  installationKind,
  isRemoving,
  OFFICIAL_PLUGIN_ID,
} from "@/pages/PluginHub/model";
import type {
  RestartChange,
  TranslationAvailableEngine,
  TranslationState,
} from "@/types/translation";

/** The kinds of plugin the Translation page is about: engines and their back ends. */
const TRANSLATION_KINDS = new Set(["translator", "translator_backend"]);

export interface EngineInstall {
  /** Ask about installing an engine. Nothing is sent until the person agrees in the dialog. */
  begin: (engine: TranslationAvailableEngine) => void;
  /** The consent dialog. Render it once, next to the buttons that call `begin`. */
  dialog: ReactElement;
  /**
   * An install is going ahead or its dialog is open, with a refusal in it that must not be lost. Buttons
   * that start another install, and a wizard step's own way out, wait on it.
   */
  busy: boolean;
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
  return { begin, dialog: flow.dialog, busy: flow.busy };
}

/**
 * The state with each engine whose plugin the server says waits on a restart marked as such, so a
 * finished install reads as "pending restart" and not as a plugin that is broken. The mark comes from the
 * plugin's `needs_restart` and nothing else. `waiting` is how many translator plugins wait on a restart,
 * for the banner, whether or not an engine of theirs is still listed.
 * `installedEnginePlugins` comes from the same lookup and stays undefined until that lookup has data.
 */
export function useRestartAware(state: TranslationState | undefined): {
  state: TranslationState | undefined;
  waiting: number;
  installedEnginePlugins: ReadonlySet<string> | undefined;
} {
  const installations = usePluginInstallations();
  // What the restart will do to each plugin that waits on one, read from the installation: a removal is
  // staged as a removed row, an update as a staged release beside an active one, an install as a staged
  // release with nothing active.
  const { ids, removing, installedEnginePlugins } = useMemo(() => {
    const changes = new Map<string, RestartChange>();
    const removing = new Set<string>();
    const installedEnginePlugins = installations.data && new Set<string>();
    for (const item of installations.data ?? []) {
      const kind = installationKind(item);
      if (!TRANSLATION_KINDS.has(kind)) {
        continue;
      }
      if (kind === "translator" && item.state !== "removed") {
        installedEnginePlugins?.add(item.provider_id);
      }
      if (isRemoving(item)) {
        removing.add(item.provider_id);
      }
      if (item.needs_restart === true) {
        changes.set(
          item.provider_id,
          item.state === "removed"
            ? "removal"
            : item.active_version
              ? "update"
              : "install",
        );
      }
    }
    return { ids: changes, removing, installedEnginePlugins };
  }, [installations.data]);
  return useMemo(() => {
    if (!state || (ids.size === 0 && removing.size === 0)) {
      return { state, waiting: ids.size, installedEnginePlugins };
    }
    const mark = <T extends { plugin_id: string }>(engine: T): T => {
      const change = ids.get(engine.plugin_id);
      return change ? { ...engine, awaiting_restart: change } : engine;
    };
    const instances = state.instances.map((engine) => {
      const marked = mark(engine);
      return removing.has(engine.plugin_id)
        ? { ...marked, removing: true }
        : marked;
    });
    const available = state.available_engines?.map(mark);
    return {
      state: {
        ...state,
        instances,
        ...(available ? { available_engines: available } : {}),
      },
      // Counted from the installations, not from the engines the page lists: a removal that deleted its
      // data has no engine left, and a plugin whose release is still active is in neither list.
      waiting: ids.size,
      installedEnginePlugins,
    };
  }, [state, ids, removing, installedEnginePlugins]);
}
