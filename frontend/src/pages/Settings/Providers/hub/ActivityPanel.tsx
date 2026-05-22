import { FunctionComponent, useEffect, useMemo, useState } from "react";
import {
  ActionIcon,
  Group,
  SegmentedControl,
  Stack,
  Switch,
  Tooltip,
} from "@mantine/core";
import { faListCheck, faRotate } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQueryClient } from "@tanstack/react-query";
import { useProviderHubJobs } from "@/apis/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import type { ProviderHubJob } from "@/apis/raw/providerHub";
import { ActivityRow } from "@/pages/Settings/Providers/hub/components/ActivityRow";
import { EmptyState } from "@/pages/Settings/Providers/hub/components/EmptyState";
import { SearchBar } from "@/pages/Settings/Providers/hub/components/SearchBar";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

type StateFilter = "all" | "running" | "failed";

export const ActivityPanel: FunctionComponent = () => {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<StateFilter>("all");
  const [live, setLive] = useState(false);

  const client = useQueryClient();
  const jobs = useProviderHubJobs();
  // Depending on the whole `jobs` object retriggers the effect on every
  // refetch, which then calls refetch() again and busy-loops the API.
  // Hold the function reference outside the dep array.
  const refetch = jobs.refetch;

  // Wire Live toggle to react-query refetchInterval at runtime.
  useEffect(() => {
    const interval = live ? 5000 : false;
    client.setQueryDefaults([QueryKeys.ProviderHub, QueryKeys.Jobs], {
      refetchInterval: interval,
    });
    if (live) refetch();
  }, [live, client, refetch]);

  const sorted = useMemo(() => {
    const list = [...(jobs.data ?? [])];
    list.sort((a, b) => {
      const ta = new Date(a.updated_at ?? a.created_at ?? 0).getTime();
      const tb = new Date(b.updated_at ?? b.created_at ?? 0).getTime();
      return tb - ta;
    });
    return list;
  }, [jobs.data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return sorted.filter((job: ProviderHubJob) => {
      if (
        filter === "running" &&
        job.state !== "running" &&
        job.state !== "pending"
      )
        return false;
      if (filter === "failed" && job.state !== "failed") return false;
      if (!q) return true;
      const hay = [job.action, job.message, job.state]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [sorted, query, filter]);

  return (
    <Stack gap="md">
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.eyebrow}>Activity</div>
          <h2 className={styles.panelTitle}>Background jobs</h2>
          <p className={styles.panelDescription}>
            Catalog refreshes, installs, update checks, and other provider hub
            actions. Click a row to see full details.
          </p>
        </div>
      </div>

      <Group className={styles.toolbar}>
        <SegmentedControl
          value={filter}
          onChange={(v) => setFilter(v as StateFilter)}
          data={[
            { label: "All", value: "all" },
            { label: "Running", value: "running" },
            { label: "Failed", value: "failed" },
          ]}
          size="xs"
        />
        <SearchBar
          value={query}
          onChange={setQuery}
          placeholder="Search activity"
          ariaLabel="Search activity"
        />
        <div className={styles.toolbarSpacer} />
        <Switch
          label="Live"
          size="sm"
          checked={live}
          onChange={(e) => setLive(e.currentTarget.checked)}
        />
        <Tooltip label="Refresh">
          <ActionIcon
            variant="default"
            onClick={() => jobs.refetch()}
            aria-label="Refresh activity"
          >
            <FontAwesomeIcon icon={faRotate} />
          </ActionIcon>
        </Tooltip>
      </Group>

      {filtered.length === 0 ? (
        <EmptyState
          icon={faListCheck}
          title="No matching activity"
          body="Background jobs from the Marketplace and Updates tabs appear here."
        />
      ) : (
        <Stack gap={4}>
          {filtered.map((job, i) => (
            <ActivityRow key={job.id ?? i} job={job} />
          ))}
        </Stack>
      )}
    </Stack>
  );
};
