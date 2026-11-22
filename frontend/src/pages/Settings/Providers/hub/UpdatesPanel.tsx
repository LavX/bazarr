import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Button, Group, Stack, Text } from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import { faCircleCheck, faRotate } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { isAxiosError } from "axios";
import {
  useProviderHubApplyUpdate,
  useProviderHubCheckUpdates,
} from "@/apis/hooks";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type {
  ProviderHubCatalog,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { EmptyState } from "@/components/hub/EmptyState";
import {
  getLatestCatalogEntry,
  summarizeUpdates,
} from "@/components/hub/utils";
import { useGatedAction } from "@/components/modals/PasswordConfirmModal";
import { usePluginConsentFlow } from "@/components/plugin-consent";
import { notification } from "@/modules/notification";
import { UpdateCard } from "@/pages/Settings/Providers/hub/components/UpdateCard";
import styles from "@/components/hub/hub.module.scss";

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
  const runGated = useGatedAction();

  const [applyingIds, setApplyingIds] = useState<Set<string>>(new Set());
  const [applyingAll, setApplyingAll] = useState(false);

  const stagedRows = summary.pendingRestart;
  const availableRows = summary.available;

  // The backend gates an update behind the password confirmation, so the
  // apply runs through the gated action: with login on and no live window the
  // prompt appears here rather than as a refusal toast after the request.
  const applyWithoutPreview = useCallback(
    async (id: string) => {
      await runGated(async () => {
        await applyUpdate.mutateAsync(id).catch(() => undefined);
      }, "apply this provider update");
    },
    [applyUpdate, runGated],
  );
  const { begin, dialog, updating, busy } = usePluginConsentFlow({
    onUpdateWithoutPreview: applyWithoutPreview,
  });

  // One password confirmation covers the batch. Each update that adds access needs individual consent.
  const applyAll = useCallback(async () => {
    const candidates = availableRows
      .map((row) => row.provider_id)
      .filter((id) => !applyingIds.has(id));
    if (candidates.length === 0) return;
    setApplyingAll(true);
    try {
      const previews = await Promise.all(
        candidates.map(async (id) => {
          try {
            const delta = await withInlineErrors(() =>
              api.plugins.updateConsent(id),
            );
            return { id, status: delta.needs_consent ? "review" : "ready" };
          } catch (error) {
            return {
              id,
              status:
                isAxiosError(error) && error.response?.status === 404
                  ? "ready"
                  : "unavailable",
            };
          }
        }),
      );
      if (previews.some(({ status }) => status === "review")) {
        showNotification(
          notification.info(
            "Some updates need consent",
            "Apply those provider updates individually.",
          ),
        );
      }
      if (previews.some(({ status }) => status === "unavailable")) {
        showNotification(
          notification.warn(
            "Some updates could not be checked",
            "Try again to check and apply those provider updates.",
          ),
        );
      }
      const ids = previews
        .filter(({ status }) => status === "ready")
        .map(({ id }) => id);
      if (ids.length === 0) return;
      await runGated(async () => {
        setApplyingIds((prev) => {
          const next = new Set(prev);
          ids.forEach((id) => next.add(id));
          return next;
        });
        try {
          // The job checks consent again against the catalog it actually stages.
          await Promise.allSettled(
            ids.map((id) => applyUpdate.mutateAsync(id)),
          );
        } finally {
          setApplyingIds((prev) => {
            const next = new Set(prev);
            ids.forEach((id) => next.delete(id));
            return next;
          });
        }
      }, "apply these provider updates");
    } finally {
      setApplyingAll(false);
    }
  }, [applyUpdate, applyingIds, availableRows, runGated]);

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
                  const latest = getLatestCatalogEntry(
                    catalog,
                    p.provider_id,
                    p.source_id ?? null,
                  );
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
                <Button
                  size="xs"
                  variant="light"
                  loading={applyingAll}
                  disabled={busy}
                  onClick={applyAll}
                  leftSection={<FontAwesomeIcon icon={faRotate} />}
                >
                  Apply all
                </Button>
              </Group>
              <div className={styles.cardGrid}>
                {availableRows.map((provider) => (
                  <UpdateCard
                    key={provider.provider_id}
                    provider={provider}
                    latest={getLatestCatalogEntry(
                      catalog,
                      provider.provider_id,
                      provider.source_id ?? null,
                    )}
                    onApply={(id) =>
                      void begin({
                        action: "update",
                        pluginId: id,
                        name: provider.name,
                      })
                    }
                    isApplying={
                      applyingIds.has(provider.provider_id) ||
                      updating === provider.provider_id
                    }
                    disabled={applyingAll || busy}
                  />
                ))}
              </div>
            </Stack>
          )}
        </Stack>
      )}
      {dialog}
    </Stack>
  );
};
