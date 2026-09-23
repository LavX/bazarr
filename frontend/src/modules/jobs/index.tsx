import { FunctionComponent, useState } from "react";
import { Button, Group, Stack, Text } from "@mantine/core";
import { hideNotification, showNotification } from "@mantine/notifications";
import api from "@/apis/raw";
import { notification } from "@/modules/task";

/**
 * The standard outcome of a job: one notification when it finishes, and the
 * same actions in the Jobs drawer.
 *
 * A job names its failure (`error`) and what can be done with its result
 * (`action`) on the backend. A feature teaches the frontend how to run an
 * action kind with `registerJobAction`; nothing here knows which feature a
 * job belongs to.
 */
type JobActionHandler = (
  action: System.JobAction,
  job: System.Jobs,
) => Promise<void> | void;

const handlers = new Map<string, JobActionHandler>();

export function registerJobAction(kind: string, handler: JobActionHandler) {
  handlers.set(kind, handler);
  return () => {
    if (handlers.get(kind) === handler) handlers.delete(kind);
  };
}

function runnable(job: System.Jobs) {
  return job.status === "completed" &&
    !!job.action &&
    handlers.has(job.action.kind)
    ? job.action
    : null;
}

function errorMessage(error: unknown, fallback: string) {
  const data = (error as { response?: { data?: unknown } }).response?.data;
  if (data && typeof data === "object" && "message" in data) {
    const message = (data as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return fallback;
}

export async function runJobAction(job: System.Jobs) {
  const action = runnable(job);
  if (!action) return;
  try {
    await handlers.get(action.kind)!(action, job);
  } catch (error) {
    showNotification(
      notification.error(
        job.job_name,
        errorMessage(error, `${action.label} failed. Try again.`),
      ),
    );
  }
}

export async function retryJob(job: System.Jobs) {
  try {
    await api.system.retryJob(job.job_id);
  } catch (error) {
    showNotification(
      notification.error(
        job.job_name,
        errorMessage(error, "This job can no longer be retried."),
      ),
    );
  }
}

/** The buttons a finished job offers: its own action, or Retry. */
export const JobActions: FunctionComponent<{
  job: System.Jobs;
  onDone?: () => void;
}> = ({ job, onDone }) => {
  const [busy, setBusy] = useState(false);
  const action = runnable(job);
  const retry = job.status === "failed" && job.retryable;
  if (!action && !retry) return null;
  const run = async () => {
    setBusy(true);
    try {
      await (action ? runJobAction(job) : retryJob(job));
      onDone?.();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Group gap="xs">
      <Button
        size="compact-sm"
        variant="light"
        color={action ? "brand" : "red"}
        loading={busy}
        onClick={() => void run()}
      >
        {action ? action.label : "Retry"}
      </Button>
    </Group>
  );
};

const notified = new Set<number>();

/**
 * Tell the user a job finished, once. Called from the jobs socket handler
 * with the job's fresh state, so only a finish that happens while the app is
 * open is announced: a reload lists old finished jobs without toasting them.
 * A failure is always announced; a completion only when it offers an action,
 * so routine scheduled jobs stay quiet.
 */
export function notifyJobOutcome(job: System.Jobs) {
  if (job.status !== "completed" && job.status !== "failed") return;
  if (notified.has(job.job_id)) return;
  if (job.status === "completed" && !runnable(job)) return;
  notified.add(job.job_id);
  const id = `job-outcome-${job.job_id}`;
  const failed = job.status === "failed";
  showNotification({
    id,
    title: job.job_name,
    color: failed ? "red" : "green",
    autoClose: failed ? 12 * 1000 : 20 * 1000,
    message: (
      <Stack gap={6} mt={4}>
        <Text size="sm">
          {failed
            ? (job.error?.message ?? "The job failed.")
            : job.progress_message || "Finished."}
        </Text>
        <JobActions job={job} onDone={() => hideNotification(id)} />
      </Stack>
    ),
  });
}
