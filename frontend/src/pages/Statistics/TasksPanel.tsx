import { FunctionComponent, useMemo } from "react";
import {
  Badge,
  Group,
  Progress,
  SimpleGrid,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import { useSystemJobs, useSystemTasks } from "@/apis/hooks";
import PanelCard from "./components/PanelCard";
import StatTile from "./components/StatTile";
import { summarizeJobQueue } from "./utils";

const TasksPanel: FunctionComponent = () => {
  const { data: tasks } = useSystemTasks();
  const { data: jobs } = useSystemJobs();

  const queue = useMemo(() => summarizeJobQueue(jobs ?? []), [jobs]);
  const scheduled = tasks ?? [];
  const running = scheduled.filter((t) => t.job_running);
  const activeJobs = (jobs ?? []).filter((j) => j.status === "running");

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
        <StatTile
          label="Tasks scheduled"
          value={scheduled.length}
          hint="recurring jobs"
        />
        <StatTile
          label="Tasks running"
          value={running.length}
          color={running.length > 0 ? "blue" : undefined}
          hint="right now"
        />
        <StatTile
          label="Queue depth"
          value={queue.pending}
          hint="jobs waiting to start"
        />
        <StatTile
          label="Queue failures"
          value={queue.failedSaturated ? "10+" : queue.failed}
          color={queue.failed > 0 ? "red" : undefined}
          hint="recent only, queue keeps 10"
        />
      </SimpleGrid>

      {activeJobs.length > 0 && (
        <PanelCard title="In progress">
          <Stack gap="sm">
            {activeJobs.map((job) => (
              <div key={job.job_id}>
                <Group justify="space-between" gap="xs">
                  <Text size="sm">{job.job_name}</Text>
                  <Text size="xs" c="dimmed">
                    {job.progress_message}
                  </Text>
                </Group>
                {job.is_progress && job.progress_max > 0 && (
                  <Progress
                    mt={4}
                    value={(job.progress_value / job.progress_max) * 100}
                  />
                )}
              </div>
            ))}
          </Stack>
        </PanelCard>
      )}

      <PanelCard
        title="Schedule"
        caveat="Next-run times come from the scheduler as text. Bazarr keeps no run history, so past durations and success rates cannot be shown."
      >
        {scheduled.length === 0 ? (
          <Text size="sm" c="dimmed">
            No scheduled tasks.
          </Text>
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Task</Table.Th>
                <Table.Th>Interval</Table.Th>
                <Table.Th>Next run</Table.Th>
                <Table.Th>State</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {scheduled.map((t) => (
                <Table.Tr key={t.job_id}>
                  <Table.Td>
                    <Text size="sm">{t.name}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {t.interval}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {t.next_run_in}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {t.job_running ? (
                      <Badge color="blue" variant="light">
                        running
                      </Badge>
                    ) : (
                      <Badge color="gray" variant="light">
                        idle
                      </Badge>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </PanelCard>
    </Stack>
  );
};

export default TasksPanel;
