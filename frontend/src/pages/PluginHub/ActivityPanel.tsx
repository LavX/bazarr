import { FunctionComponent, useMemo } from "react";
import { ActionIcon, Group, Stack, Tooltip } from "@mantine/core";
import { faListCheck, faRotate } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { usePluginJobs } from "@/apis/hooks/plugins";
import { ActivityRow } from "@/components/hub/ActivityRow";
import { EmptyState } from "@/components/hub/EmptyState";
import styles from "@/components/hub/hub.module.scss";

/**
 * The jobs of the plugins this page shows. A job aimed at a subtitle provider belongs to the Subtitle
 * Hub, so only jobs for the given plugin ids, and the catalog jobs that are no one plugin's, are listed.
 */
export const ActivityPanel: FunctionComponent<{ pluginIds: Set<string> }> = ({
  pluginIds,
}) => {
  const jobs = usePluginJobs();
  const shown = useMemo(() => {
    const list = (jobs.data ?? []).filter((job) =>
      job.target_kind === "provider"
        ? job.target_id !== null &&
          job.target_id !== undefined &&
          pluginIds.has(job.target_id)
        : job.target_kind === "system",
    );
    return list.sort((a, b) => {
      const ta = new Date(a.updated_at ?? a.created_at ?? 0).getTime();
      const tb = new Date(b.updated_at ?? b.created_at ?? 0).getTime();
      return tb - ta;
    });
  }, [jobs.data, pluginIds]);

  return (
    <Stack gap="md">
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.eyebrow}>Activity</div>
          <h2 className={styles.panelTitle}>Plugin activity</h2>
          <p className={styles.panelDescription}>
            Installs, updates and catalog checks for the plugins on this page.
            Click a row to see its details.
          </p>
        </div>
        <Group>
          <Tooltip label="Refresh">
            <ActionIcon
              variant="default"
              onClick={() => void jobs.refetch()}
              aria-label="Refresh activity"
            >
              <FontAwesomeIcon icon={faRotate} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </div>
      {shown.length === 0 ? (
        <EmptyState
          icon={faListCheck}
          title="No plugin activity yet"
          body="Installs, updates and catalog checks appear here."
        />
      ) : (
        <Stack gap={4}>
          {shown.map((job, index) => (
            <ActivityRow key={job.id ?? index} job={job} />
          ))}
        </Stack>
      )}
    </Stack>
  );
};
