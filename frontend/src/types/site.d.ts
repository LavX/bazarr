declare namespace Server {
  interface Notification {
    type: "error" | "warning" | "info";
    id: string;
    message: string;
    timeout: number;
  }
}

declare namespace Manager {
  interface Jobs {
    job_id: number;
    progress_value: number;
    progress_max: number;
    progress_message: string;
    status: string;
    error?: System.JobError | null;
    action?: System.JobAction | null;
  }
}

declare namespace Site {
  type Status = "uninitialized" | "unauthenticated" | "initialized" | "error";
}
