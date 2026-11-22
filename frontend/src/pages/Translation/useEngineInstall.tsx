import { ReactElement, useCallback, useEffect, useMemo, useRef } from "react";
import { usePluginInstallations } from "@/apis/hooks/plugins";
import {
  type useTranslationState,
  useTranslationSummary,
} from "@/apis/hooks/translation";
import type { PluginInstallation } from "@/apis/raw/plugins";
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
import { engineUnavailable } from "./model";

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

/** A first lookup can reveal that the cached settings snapshot is already behind. */
function installationMismatch(
  state: TranslationState,
  engineRows: ReadonlyMap<string, PluginInstallation>,
): boolean {
  return (
    state.instances.some((engine) => {
      if (engine.legacy) return false;
      const row = engineRows.get(engine.plugin_id);
      if (!row) return engine.plugin !== null;
      if (row.state === "removed") {
        return (
          engine.plugin?.active === true ||
          state.engine === engine.id ||
          state.fallback_engine === engine.id
        );
      }
      return row.active_version
        ? engine.plugin?.version !== row.active_version
        : engine.plugin?.active === true;
    }) ||
    (state.available_engines ?? []).some((engine) => {
      const row = engineRows.get(engine.plugin_id);
      return Boolean(row?.active_version || row?.staged_version);
    })
  );
}

/** Apply installation changes at once and ask for the canonical pointers, settings and catalog offers. */
export function useRestartAware({
  data: state,
  refetch,
}: Pick<ReturnType<typeof useTranslationState>, "data" | "refetch">): {
  state: TranslationState | undefined;
  waiting: number;
} {
  const installations = usePluginInstallations();
  const previousLifecycle = useRef<string | undefined>(undefined);
  // What the restart will do to each plugin that waits on one, read from the installation: a removal is
  // staged as a removed row, an update as a staged release beside an active one, an install as a staged
  // release with nothing active.
  const snapshot = useMemo(() => {
    if (!installations.data) return undefined;
    const changes = new Map<string, RestartChange>();
    const removing = new Set<string>();
    const engineRows = new Map<string, PluginInstallation>();
    const lifecycle = [];
    const rows = installations.data
      .filter((item) => TRANSLATION_KINDS.has(installationKind(item)))
      .sort((a, b) => a.provider_id.localeCompare(b.provider_id));
    for (const item of rows) {
      const kind = installationKind(item);
      if (kind === "translator") {
        engineRows.set(item.provider_id, item);
      }
      lifecycle.push([
        item.provider_id,
        kind,
        item.state,
        item.active_version,
        item.staged_version,
        item.needs_restart === true,
        item.enabled !== false,
        item.blocked_reason,
      ]);
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
    return {
      changes,
      removing,
      engineRows,
      lifecycle: JSON.stringify(lifecycle),
    };
  }, [installations.data]);

  useEffect(() => {
    if (!snapshot || !state) return;
    const previous = previousLifecycle.current;
    if (previous === snapshot.lifecycle) return;
    previousLifecycle.current = snapshot.lifecycle;
    if (
      previous !== undefined ||
      installationMismatch(state, snapshot.engineRows)
    ) {
      void refetch();
    }
  }, [snapshot, state, refetch]);

  return useMemo(() => {
    if (!state || !snapshot) {
      return { state, waiting: snapshot?.changes.size ?? 0 };
    }
    const { changes, removing, engineRows } = snapshot;
    const mark = <T extends { plugin_id: string }>(engine: T): T => {
      const change = changes.get(engine.plugin_id);
      return change ? { ...engine, awaiting_restart: change } : engine;
    };
    const instances = state.instances.map((engine) => {
      const marked = mark(engine);
      if (engine.legacy) return marked;
      if (removing.has(engine.plugin_id)) return { ...marked, removing: true };
      if (engineRows.has(engine.plugin_id)) return marked;
      // Match the missing-plugin state that a fresh Translation response would report.
      // Keep the engine instance and its saved connections for a possible reinstall.
      return {
        ...marked,
        plugin: null,
        schema: null,
        configured: false,
        official: false,
      };
    });
    const available = state.available_engines
      ?.filter(
        (offer) =>
          !removing.has(offer.plugin_id) &&
          (!engineRows.get(offer.plugin_id)?.active_version ||
            changes.has(offer.plugin_id)),
      )
      .map(mark);
    const usablePointer = (id: number | null) =>
      instances.some((engine) => engine.id === id && !engineUnavailable(engine))
        ? id
        : null;
    return {
      state: {
        ...state,
        engine: usablePointer(state.engine),
        // The server may pick a different fallback. The refresh will supply it.
        // eslint-disable-next-line camelcase -- transport name
        fallback_engine: usablePointer(state.fallback_engine),
        instances,
        ...(available ? { available_engines: available } : {}),
      },
      // Counted from the installations, not from the engines the page lists: a removal that deleted its
      // data has no engine left, and a plugin whose release is still active is in neither list.
      waiting: changes.size,
    };
  }, [state, snapshot]);
}
