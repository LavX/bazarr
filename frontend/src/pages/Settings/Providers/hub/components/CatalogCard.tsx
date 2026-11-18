import { FunctionComponent, useMemo } from "react";
import { ActionIcon, Badge, Button, Menu, Tooltip } from "@mantine/core";
import {
  faCheck,
  faDownload,
  faEllipsis,
  faRotate,
  faScrewdriverWrench,
  faTrash,
  faVial,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type {
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { CapabilityStatus } from "@/components/hub/CapabilityStatus";
import { HubCard } from "@/components/hub/HubCard";
import { ProviderStatusBadge } from "@/components/hub/StatusBadge";
import { TrustBadge } from "@/components/hub/TrustBadge";
import {
  isAwaitingRestart,
  parseManifest,
  usesAntiCaptcha,
  usesFlaresolverr,
} from "@/components/hub/utils";
import { AntiCaptchaBadge } from "@/pages/Settings/Providers/hub/components/AntiCaptchaBadge";
import { FlareSolverrBadge } from "@/pages/Settings/Providers/hub/components/FlareSolverrBadge";
import { TranslationQuotaLine } from "@/pages/Settings/Providers/hub/components/TranslationQuotaLine";
import {
  AUTH_LABEL,
  detectAuthFromManifest,
  getLanguageName,
  getLanguagesFromManifest,
} from "@/pages/Settings/Providers/meta";
import { getProviderLanguages } from "@/pages/Settings/Providers/provider-languages";
import styles from "@/components/hub/hub.module.scss";

const LANGUAGE_CHIP_THRESHOLD = 3;

interface CatalogCardProps {
  entry: ProviderHubCatalogEntry;
  installed: ProviderHubInstallation | null;
  onInstall: (entry: ProviderHubCatalogEntry) => void;
  isInstalling: boolean;
  onTest?: (id: string) => void;
  onUninstall?: (id: string) => void;
  onRepair?: (id: string) => void;
  isTesting?: boolean;
  isRepairing?: boolean;
  /** True for providers installed from an uploaded local package, not a catalog. */
  isLocal?: boolean;
}

type CtaState =
  | "install"
  | "installed"
  | "update"
  | "restart"
  | "repair"
  | "broken"
  | "blocked";

function isPendingRemoval(installed: ProviderHubInstallation | null) {
  return installed?.needs_restart === true && installed.state === "removed";
}

function isPendingInstall(installed: ProviderHubInstallation | null) {
  return (
    installed?.needs_restart === true &&
    installed.state === "staged" &&
    !installed.active_version
  );
}

function isPendingUpdate(installed: ProviderHubInstallation | null) {
  return (
    installed?.needs_restart === true &&
    installed.state === "staged" &&
    Boolean(installed.active_version)
  );
}

function deriveCta(
  entry: ProviderHubCatalogEntry,
  installed: ProviderHubInstallation | null,
  manifestValid: boolean,
): CtaState {
  if (!installed && !manifestValid) return "broken";
  if (!installed) return entry.blocked_reason ? "blocked" : "install";
  if (isAwaitingRestart(installed)) return "restart";
  if (installed.needs_repair) return "repair";
  if (
    installed.active_version &&
    installed.active_version !== entry.version &&
    !entry.blocked_reason &&
    entry.origin !== "bundled"
  ) {
    return "update";
  }
  return "installed";
}

export const CatalogCard: FunctionComponent<CatalogCardProps> = ({
  entry,
  installed,
  onInstall,
  isInstalling,
  onTest,
  onUninstall,
  onRepair,
  isTesting,
  isRepairing = false,
  isLocal = false,
}) => {
  const manifest = useMemo(() => parseManifest(entry), [entry]);
  const cta = deriveCta(entry, installed, manifest !== null);
  const displayName = entry.name ?? installed?.name ?? entry.provider_id;
  const pendingRemoval = isPendingRemoval(installed);
  const pendingInstall = isPendingInstall(installed);
  const pendingUpdate = isPendingUpdate(installed);
  const canShowActions = installed !== null && !pendingRemoval;
  const canTest = installed?.state === "active" && !installed.needs_restart;
  const removeLabel = pendingInstall
    ? "Cancel staged install"
    : pendingUpdate
      ? "Uninstall instead"
      : "Uninstall";

  const ctaButton = (() => {
    switch (cta) {
      case "installed":
        return (
          <Button
            size="xs"
            variant="light"
            color="green"
            disabled
            leftSection={<FontAwesomeIcon icon={faCheck} />}
          >
            Installed
          </Button>
        );
      case "update":
        return (
          <Button
            size="xs"
            variant="light"
            color="yellow"
            loading={isInstalling}
            onClick={() => onInstall(entry)}
            leftSection={<FontAwesomeIcon icon={faRotate} />}
          >
            Update
          </Button>
        );
      case "restart":
        return (
          <Tooltip
            label={
              pendingRemoval
                ? "Restart Bazarr+ to remove this plugin"
                : pendingUpdate
                  ? "Restart Bazarr+ to activate the staged update"
                  : "Restart Bazarr+ to activate this plugin"
            }
          >
            <span className={styles.hubCardPendingText}>Pending restart</span>
          </Tooltip>
        );
      case "repair":
        return (
          <Tooltip
            label={
              installed?.last_error ??
              "The plugin's Python environment is missing or broken"
            }
            multiline
            w={260}
          >
            <Button
              size="xs"
              variant="light"
              color="red"
              loading={isRepairing}
              disabled={!onRepair}
              onClick={() => installed && onRepair?.(installed.provider_id)}
              leftSection={<FontAwesomeIcon icon={faScrewdriverWrench} />}
            >
              Repair
            </Button>
          </Tooltip>
        );
      case "broken":
        return (
          <Tooltip label="This catalog entry is missing a valid manifest">
            <Button size="xs" variant="light" color="gray" disabled>
              Unavailable
            </Button>
          </Tooltip>
        );
      case "blocked":
        return (
          <Tooltip label={entry.blocked_reason} multiline w={260}>
            <Button size="xs" variant="light" color="gray" disabled>
              Unavailable
            </Button>
          </Tooltip>
        );
      case "install":
      default:
        return (
          <Button
            size="xs"
            variant="light"
            loading={isInstalling}
            onClick={() => onInstall(entry)}
            leftSection={<FontAwesomeIcon icon={faDownload} />}
          >
            Install
          </Button>
        );
    }
  })();

  const sourceLabel = isLocal
    ? "local package"
    : (entry.source_name ?? entry.source ?? "Unknown source");
  const description =
    (manifest?.description as string | undefined) ??
    (manifest?.summary as string | undefined) ??
    "";

  const auth = detectAuthFromManifest(manifest);
  const manifestLangs = getLanguagesFromManifest(manifest);
  const fallbackLangs = getProviderLanguages(entry.provider_id);
  const languages = manifestLangs.length > 0 ? manifestLangs : fallbackLangs;
  const showAllLangs = languages.length <= LANGUAGE_CHIP_THRESHOLD;
  const langTooltip = languages.map((c) => getLanguageName(c)).join(", ");

  return (
    <HubCard
      name={displayName}
      meta={
        <>
          v{entry.version} from {sourceLabel}
        </>
      }
      menu={
        canShowActions && installed ? (
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon
                variant="subtle"
                color="gray"
                aria-label={`${displayName} actions`}
              >
                <FontAwesomeIcon icon={faEllipsis} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item
                leftSection={<FontAwesomeIcon icon={faVial} />}
                onClick={() => onTest?.(installed.provider_id)}
                disabled={!onTest || !canTest || isTesting}
              >
                {canTest ? "Test connection" : "Test after restart"}
              </Menu.Item>
              <Menu.Divider />
              <Menu.Item
                color="red"
                leftSection={<FontAwesomeIcon icon={faTrash} />}
                onClick={() => onUninstall?.(installed.provider_id)}
                disabled={!onUninstall}
              >
                {removeLabel}
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        ) : undefined
      }
      description={description}
      note={installed?.trust_note}
      extra={
        <>
          <TranslationQuotaLine status={installed?.runtime_status} />
          <CapabilityStatus capabilities={installed?.capabilities} />
        </>
      }
      chips={
        <>
          <Badge size="xs" variant="outline" color="gray">
            {AUTH_LABEL[auth]}
          </Badge>
          {languages.length > 0 &&
            showAllLangs &&
            languages.map((code) => (
              <Badge key={code} size="xs" variant="outline" color="gray">
                {getLanguageName(code)}
              </Badge>
            ))}
          {languages.length > LANGUAGE_CHIP_THRESHOLD && (
            <Badge size="xs" variant="outline" color="gray" title={langTooltip}>
              {languages.length} languages
            </Badge>
          )}
        </>
      }
      pills={
        <>
          {installed && (
            <ProviderStatusBadge
              state={installed.state}
              pendingRestart={isAwaitingRestart(installed)}
              activeVersion={installed.active_version}
              needsRepair={installed.needs_repair}
            />
          )}
          {isLocal ? (
            <Tooltip label="Installed from an uploaded local package, not a catalog source. Never trusted; cannot replace a built-in provider.">
              <Badge size="xs" variant="light" color="grape">
                Local
              </Badge>
            </Tooltip>
          ) : (
            <TrustBadge trusted={entry.trusted} />
          )}
          {usesAntiCaptcha(entry) && <AntiCaptchaBadge />}
          {usesFlaresolverr(entry) && <FlareSolverrBadge />}
        </>
      }
      action={ctaButton}
    />
  );
};
