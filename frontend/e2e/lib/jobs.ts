/**
 * Following jobs on the queue through the API, and the page's live channel
 * that announces them.
 */
import type { APIRequestContext, Page } from "@playwright/test";
import { expect } from "@playwright/test";

export type JobStatus = "pending" | "running" | "failed" | "completed";

export interface Job {
  job_id: number;
  job_name: string;
  status: JobStatus;
  last_run_time: string;
  error: { reason: string; message: string } | null;
}

/** How long a job that needs no network is given to finish. */
const JOB_CAP_MS = 30_000;

export async function getJob(
  api: APIRequestContext,
  id: number,
): Promise<Job | null> {
  const response = await api.get(`/api/system/jobs?id=${id}`);
  expect(response.ok()).toBe(true);
  const { data } = (await response.json()) as { data: Job[] };
  return data.find((job) => job.job_id === id) ?? null;
}

/** Waits for a job to end in `status` and returns it as the API reports it. */
export async function waitForJob(
  api: APIRequestContext,
  id: number,
  status: "failed" | "completed",
): Promise<Job> {
  await expect
    .poll(async () => (await getJob(api, id))?.status, {
      message: `job ${id} should end ${status}`,
      timeout: JOB_CAP_MS,
    })
    .toBe(status);
  return (await getJob(api, id)) as Job;
}

/**
 * Resolves once the page's Socket.IO channel is connected, which is when a job
 * that finishes gets announced to it. Call it before the navigation that
 * opens the page. The connect answer is the "40" packet, and it comes back on
 * a long-poll request carrying the session id.
 */
export function liveChannel(page: Page) {
  return page.waitForResponse(async (response) => {
    const url = response.url();
    if (!url.includes("/api/socket.io/") || !url.includes("sid=")) {
      return false;
    }
    if (response.request().method() !== "GET" || !response.ok()) return false;
    return (await response.text())
      .split("\x1e")
      .some((packet) => packet.startsWith("40"));
  });
}
