/* eslint-disable camelcase */
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import TasksPanel from "@/pages/Statistics/TasksPanel";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

const task = (over: Partial<System.Task> = {}): System.Task => ({
  job_id: "update_series",
  name: "Update Series",
  interval: "every 6 hours",
  job_running: false,
  next_run_in: "in 2 hours",
  next_run_time: "14:00",
  ...over,
});

const mock = (tasks: System.Task[] = [], jobs: { status: string }[] = []) => {
  server.use(
    http.get("/api/system/tasks", () => HttpResponse.json({ data: tasks })),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: jobs })),
  );
};

describe("Statistics > TasksPanel", () => {
  beforeEach(() => mock());

  it("lists each scheduled task with its interval and next run", async () => {
    mock([task()]);
    customRender(<TasksPanel />);

    expect(await screen.findByText("Update Series")).toBeInTheDocument();
    expect(screen.getByText("every 6 hours")).toBeInTheDocument();
    expect(screen.getByText("in 2 hours")).toBeInTheDocument();
  });

  it("marks a task that is currently running", async () => {
    mock([task({ job_running: true })]);
    customRender(<TasksPanel />);

    // Exact match: the "Tasks running" tile label also contains "running".
    expect(await screen.findByText("running")).toBeInTheDocument();
  });

  it("marks a task that is idle as scheduled rather than running", async () => {
    mock([task({ job_running: false })]);
    customRender(<TasksPanel />);

    expect(await screen.findByText("Update Series")).toBeInTheDocument();
    expect(screen.getByText("idle")).toBeInTheDocument();
    expect(screen.queryByText("running")).not.toBeInTheDocument();
  });

  it("counts how many tasks are scheduled", async () => {
    mock([task(), task({ job_id: "b", name: "Sync Movies" })]);
    customRender(<TasksPanel />);

    // Await the value: the label renders before the tasks query resolves.
    expect(await screen.findByText("2")).toBeInTheDocument();
    expect(screen.getByText(/tasks scheduled/i)).toBeInTheDocument();
  });

  it("says so when no task is scheduled", async () => {
    customRender(<TasksPanel />);

    expect(await screen.findByText(/no scheduled tasks/i)).toBeInTheDocument();
  });

  it("states that no task run history is recorded", async () => {
    // APScheduler uses an in-memory job store and the queue keeps 10 entries,
    // so durations and past runs genuinely do not exist to chart.
    customRender(<TasksPanel />);

    expect(await screen.findByText(/no run history/i)).toBeInTheDocument();
  });

  it("shows the progress message of a running queue job", async () => {
    mock(
      [],
      [
        {
          status: "running",
          job_name: "Search wanted",
          is_progress: true,
          progress_value: 3,
          progress_max: 10,
          progress_message: "Searching episode 3 of 10",
        } as never,
      ],
    );
    customRender(<TasksPanel />);

    expect(
      await screen.findByText("Searching episode 3 of 10"),
    ).toBeInTheDocument();
  });
});
