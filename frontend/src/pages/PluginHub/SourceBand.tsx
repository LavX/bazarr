import { FunctionComponent } from "react";
import { Alert, Badge, Button } from "@mantine/core";
import { faRotate } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { usePluginRefreshCatalog } from "@/apis/hooks/plugins";
import type { PluginCatalogSource } from "@/apis/raw/plugins";
import { formatRelativeTime } from "@/components/hub/utils";
import styles from "./PluginHub.module.scss";

/** Whether the signed catalog has passed the time it stops being installable from. */
export function catalogExpired(
  source: PluginCatalogSource,
  now: Date = new Date(),
): boolean {
  if (!source.catalog_expires_at) {
    return false;
  }
  const expires = Date.parse(source.catalog_expires_at);
  return !Number.isNaN(expires) && expires < now.getTime();
}

/**
 * The official source's standing, at the top of the page: how fresh its catalog is, whether it is
 * signed, whether what is shown is the snapshot that shipped with this release, and the one thing
 * that blocks every install, an expired catalog. Plugins that are running are never touched by it.
 */
export const SourceBand: FunctionComponent<{
  source: PluginCatalogSource | undefined;
}> = ({ source }) => {
  const refresh = usePluginRefreshCatalog();
  if (!source) {
    return null;
  }
  const expired = catalogExpired(source);
  const bundled = source.catalog_status === "bundled";
  const signature =
    source.signature_verified === true
      ? "Signed catalog"
      : source.catalog_signature
        ? "Unsigned catalog"
        : null;
  return (
    <>
      {expired && (
        <Alert
          color="yellow"
          role="alert"
          mb="md"
          title="The official catalog has expired"
        >
          Nothing installs or updates from it until it refreshes. Plugins that
          are running are not touched.
          <div>
            <Button
              mt="xs"
              size="xs"
              variant="default"
              loading={refresh.isPending}
              onClick={() => refresh.mutate()}
            >
              Refresh the catalog
            </Button>
          </div>
        </Alert>
      )}
      <div
        className={styles.band}
        role="group"
        aria-label="Official catalog status"
      >
        <div className={styles.fact}>
          <span className={styles.factStrong}>{source.name}</span>
          {source.last_checked_at ? (
            <span>Checked {formatRelativeTime(source.last_checked_at)}</span>
          ) : (
            <span>Not checked yet</span>
          )}
          {signature && <span className={styles.factLabel}>{signature}</span>}
          {source.catalog_channel && (
            <Badge size="xs" variant="outline" color="gray">
              {source.catalog_channel}
            </Badge>
          )}
          {bundled && (
            <Badge size="xs" variant="light" color="gray">
              Bundled snapshot
            </Badge>
          )}
          {source.last_error && <span role="status">{source.last_error}</span>}
        </div>
        <div className={styles.bandActions}>
          <Button
            size="xs"
            variant="default"
            leftSection={<FontAwesomeIcon icon={faRotate} />}
            loading={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            Refresh catalog
          </Button>
        </div>
      </div>
    </>
  );
};
