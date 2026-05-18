import { FunctionComponent, useMemo } from "react";
import { Button, Group, Stack, Text } from "@mantine/core";
import { faCircleCheck, faRotate } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useProviderHubApplyUpdate,
  useProviderHubCheckUpdates,
} from "@/apis/hooks";
import type {
  ProviderHubCatalog,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { EmptyState } from "@/pages/Settings/Providers/hub/components/EmptyState";
import { UpdateCard } from "@/pages/Settings/Providers/hub/components/UpdateCard";
import {
  getLatestCatalogEntry,
  summarizeUpdates,
} from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface UpdatesPanelProps {
  providers: ProviderHubInstallation[] | undefined;
  catalog: ProviderHubCatalog | undefined;
}

export const UpdatesPanel: FunctionComponent<UpdatesPanelProps> = ({
  providers,
  catalog,
}) => {
  const summary = useMemo(
    () => summarizeUpdates(providers, catalog),
    [providers, catalog],
  );
  const applyUpdate = useProviderHubApplyUpdate();
  const checkUpdates = useProviderHubCheckUpdates();

  const stagedRows = summary.pendingRestart;
  const availableRows = summary.available;

  const empty = stagedRows.length === 0 && availableRows.length === 0;
  const changeCount = availableRows.length + stagedRows.length;
  const title = empty
    ? "All providers up to date"
    : stagedRows.length > 0 && availableRows.length === 0
      ? `${stagedRows.length} staged change${stagedRows.length === 1 ? "" : "s"}`
      : `${changeCount} provider change${changeCount === 1 ? "" : "s"}`;

  return (
    <Stack gap="md">
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.eyebrow}>Updates</div>
          <h2 className={styles.panelTitle}>{title}</h2>
          <p className={styles.panelDescription}>
            Review catalog updates and staged Provider Hub changes.
          </p>
        </div>
        <Button
          variant="default"
          leftSection={<FontAwesomeIcon icon={faRotate} />}
          loading={checkUpdates.isPending}
          onClick={() => checkUpdates.mutate()}
        >
          Check now
        </Button>
      </div>

      {empty ? (
        <EmptyState
          icon={faCircleCheck}
          title="All providers up to date"
          body="We will surface a banner at the top of the page when new versions land in your catalog."
        />
      ) : (
        <Stack gap="md">
          {stagedRows.length > 0 && (
            <Stack gap="xs">
              <Text size="sm" fw={600}>
                Staged for restart
              </Text>
              <div className={styles.cardGrid}>
                {stagedRows.map((p) => {
                  const latest = getLatestCatalogEntry(catalog, p.provider_id);
                  return (
                    <UpdateCard
                      key={p.provider_id}
                      provider={p}
                      latest={latest}
                      onApply={() => undefined}
                    />
                  );
                })}
              </div>
            </Stack>
          )}
          {availableRows.length > 0 && (
            <Stack gap="xs">
              <Group justify="space-between">
                <Text size="sm" fw={600}>
                  Available now
                </Text>
                <Text size="xs" c="dimmed">
                  Apply individually
                </Text>
              </Group>
              <div className={styles.cardGrid}>
                {availableRows.map((p) => {
                  const latest = getLatestCatalogEntry(catalog, p.provider_id);
                  if (!latest) return null;
                  return (
                    <UpdateCard
                      key={p.provider_id}
                      provider={p}
                      latest={latest}
                      onApply={(id) => applyUpdate.mutate(id)}
                      isApplying={applyUpdate.isPending}
                    />
                  );
                })}
              </div>
            </Stack>
          )}
        </Stack>
      )}
    </Stack>
  );
};
