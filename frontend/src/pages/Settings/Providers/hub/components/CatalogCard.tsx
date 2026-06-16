import { FunctionComponent, useMemo } from "react";
import { ActionIcon, Badge, Button, Group, Menu, Tooltip } from "@mantine/core";
import {
  faCheck,
  faDownload,
  faEllipsis,
  faPuzzlePiece,
  faRotate,
  faTrash,
  faVial,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import type {
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { AntiCaptchaBadge } from "@/pages/Settings/Providers/hub/components/AntiCaptchaBadge";
import { FlareSolverrBadge } from "@/pages/Settings/Providers/hub/components/FlareSolverrBadge";
import { ProviderStatusBadge } from "@/pages/Settings/Providers/hub/components/StatusBadge";
import { TrustBadge } from "@/pages/Settings/Providers/hub/components/TrustBadge";
import {
  parseManifest,
  requiresAntiCaptcha,
  requiresFlaresolverr,
} from "@/pages/Settings/Providers/hub/utils";
import {
  AUTH_LABEL,
  detectAuthFromManifest,
  getLanguageName,
  getLanguagesFromManifest,
} from "@/pages/Settings/Providers/meta";
import { getProviderLanguages } from "@/pages/Settings/Providers/provider-languages";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

const LANGUAGE_CHIP_THRESHOLD = 3;

interface CatalogCardProps {
  entry: ProviderHubCatalogEntry;
  installed: ProviderHubInstallation | null;
  onInstall: (entry: ProviderHubCatalogEntry) => void;
  isInstalling: boolean;
  onTest?: (id: string) => void;
  onUninstall?: (id: string) => void;
  isTesting?: boolean;
  /** True for providers installed from an uploaded local package, not a catalog. */
  isLocal?: boolean;
}

type CtaState = "install" | "installed" | "update" | "restart" | "broken";

function isPendingRemoval(installed: ProviderHubInstallation | null) {
  return installed?.pending_restart === true && installed.state === "removed";
}

function isPendingInstall(installed: ProviderHubInstallation | null) {
  return (
    installed?.pending_restart === true &&
    installed.state === "staged" &&
    !installed.active_version
  );
}

function isPendingUpdate(installed: ProviderHubInstallation | null) {
  return (
    installed?.pending_restart === true &&
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
  if (!installed) return "install";
  if (installed.pending_restart) return "restart";
  if (installed.active_version && installed.active_version !== entry.version) {
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
  isTesting,
  isLocal = false,
}) => {
  const manifest = useMemo(() => parseManifest(entry), [entry]);
  const cta = deriveCta(entry, installed, manifest !== null);
  const displayName = entry.name ?? installed?.name ?? entry.provider_id;
  const pendingRemoval = isPendingRemoval(installed);
  const pendingInstall = isPendingInstall(installed);
  const pendingUpdate = isPendingUpdate(installed);
  const canShowActions = installed !== null && !pendingRemoval;
  const canTest = installed?.state === "active" && !installed.pending_restart;
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
      case "broken":
        return (
          <Tooltip label="This catalog entry is missing a valid manifest">
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
    : (entry.source ?? entry.source_name ?? "Unknown source");
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
    <div className={clsx(styles.hubCard)}>
      <div className={styles.hubCardHeader}>
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            minWidth: 0,
          }}
        >
          <div
            aria-hidden="true"
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              background: "var(--bz-hover-bg)",
              color: "var(--bz-text-secondary)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flex: "0 0 auto",
              fontSize: 16,
            }}
          >
            <FontAwesomeIcon icon={faPuzzlePiece} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div className={styles.hubCardTitle}>{displayName}</div>
            <div className={styles.hubCardMeta}>
              v{entry.version} from {sourceLabel}
            </div>
          </div>
        </div>
        {canShowActions && installed && (
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
        )}
      </div>
      {description && (
        <div className={styles.hubCardDescription}>{description}</div>
      )}
      <div className={styles.hubCardChips}>
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
      </div>
      <div className={styles.hubCardFooter}>
        <Group gap={6} className={styles.hubCardPills}>
          {installed && (
            <ProviderStatusBadge
              state={installed.state}
              pendingRestart={installed.pending_restart}
              activeVersion={installed.active_version}
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
          {requiresAntiCaptcha(entry) && <AntiCaptchaBadge />}
          {requiresFlaresolverr(entry) && <FlareSolverrBadge />}
        </Group>
        <div className={styles.hubCardAction}>{ctaButton}</div>
      </div>
    </div>
  );
};
