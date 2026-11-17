import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faArrowUp,
  faCheckCircle,
  faCircleNotch,
  faPause,
  faScrewdriverWrench,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import type {
  PluginInstallation,
  PluginJob,
  PluginRuntime,
} from "@/apis/raw/plugins";
import type { Tone } from "@/components/hub/utils";
import { isInstalled, PluginRow } from "./model";

export type StatusKey =
  | "attention"
  | "rebuilding"
  | "installing"
  | "activating"
  | "update"
  | "active"
  | "off";

export interface PluginStatus {
  key: StatusKey;
  label: string;
  tone: Tone;
  icon: IconDefinition;
  spin?: boolean;
  /** Why it needs attention, when it does. */
  cause?: string;
  tooltip?: string;
}

const isOpen = (job: PluginJob) =>
  job.state === "pending" || job.state === "running";

/** The open job of each plugin, by plugin id and what the job does. */
export function openJobs(jobs: PluginJob[] | undefined) {
  const byPlugin = new Map<string, Set<string>>();
  for (const job of jobs ?? []) {
    if (!isOpen(job) || !job.target_id || !job.action) {
      continue;
    }
    const actions = byPlugin.get(job.target_id) ?? new Set<string>();
    actions.add(job.action);
    byPlugin.set(job.target_id, actions);
  }
  return byPlugin;
}

/** What is wrong with an installation, in the server's words, or null when nothing is. */
export function attentionCause(
  installation: PluginInstallation,
): string | null {
  if (installation.needs_repair) {
    return (
      installation.last_error ??
      "The plugin's environment is missing or broken. Use Repair to rebuild it."
    );
  }
  if (installation.state === "failed") {
    return installation.last_error ?? "The last install or update failed.";
  }
  if (installation.runtime?.phase === "broken") {
    return (
      installation.runtime.last_error ??
      "The plugin's worker could not be started."
    );
  }
  return null;
}

/**
 * One status for an installed plugin, by precedence: needs attention, rebuilding, installing,
 * activating, update available, active, off. Text and an icon carry every state, never colour alone.
 * Null for a plugin that is not installed. A plugin that needs Docker access has no place yet: the
 * server does not report it.
 */
export function pluginStatus(
  row: PluginRow,
  open: Map<string, Set<string>>,
): PluginStatus | null {
  const installation = row.installation;
  if (!installation || !isInstalled(installation)) {
    return null;
  }
  const actions = open.get(row.id);
  const cause = attentionCause(installation);
  if (cause && !actions?.has("repair")) {
    return {
      key: "attention",
      label: "Needs attention",
      tone: "danger",
      icon: faTriangleExclamation,
      cause,
    };
  }
  if (actions?.has("repair")) {
    return {
      key: "rebuilding",
      label: "Rebuilding",
      tone: "warning",
      icon: faScrewdriverWrench,
      tooltip: "Rebuilding the plugin's environment.",
    };
  }
  // The server records the staging of a new release as "stage_update", and putting it in place as "activate".
  if (actions?.has("install") || actions?.has("stage_update")) {
    return {
      key: "installing",
      label: "Installing",
      tone: "info",
      icon: faCircleNotch,
      spin: true,
    };
  }
  if (actions?.has("activate") || installation.state === "staged") {
    return {
      key: "activating",
      label: "Activating",
      tone: "info",
      icon: faCircleNotch,
      spin: true,
      tooltip: "A new release is being put in place.",
    };
  }
  if (row.update) {
    return {
      key: "update",
      label: "Update available",
      tone: "warning",
      icon: faArrowUp,
    };
  }
  if (installation.enabled === false) {
    return { key: "off", label: "Off", tone: "neutral", icon: faPause };
  }
  return {
    key: "active",
    label: "Active",
    tone: "success",
    icon: faCheckCircle,
  };
}

/** The runtime line under the status: what the plugin's worker is doing, in words. Null when it has none. */
export function runtimeLine(runtime: PluginRuntime | undefined): string | null {
  switch (runtime?.phase) {
    case "starting":
      return "Starting";
    case "healthy":
      return runtime.in_flight > 1
        ? `Healthy, ${runtime.in_flight} requests running`
        : "Healthy, running a request";
    case "idle":
      return "Idle, ready for work";
    case "backing_off": {
      const wait = Math.ceil(runtime.retry_after ?? 0);
      return wait > 0
        ? `Backing off after a failure, trying again in ${wait} s`
        : "Backing off after a failure";
    }
    case "broken":
      return "Could not start";
    case "stopping":
      return "Stopping";
    case "stopped":
      return "Stopped, starts when needed";
    default:
      return null;
  }
}
