declare namespace System {
  interface Announcements {
    text: string;
    link: string;
    hash: string;
    dismissible: boolean;
    timestamp: string;
  }

  interface Task {
    interval: string;
    job_id: string;
    job_running: boolean;
    name: string;
    next_run_in: string;
    next_run_time: string;
  }

  interface Jobs {
    job_id: number;
    job_name: string;
    status: string;
    last_run_time: string;
    is_progress: boolean;
    is_signalr: boolean;
    progress_value: number;
    progress_max: number;
    progress_message: string;
    /** Why a failed job failed, set by the job. */
    error?: JobError | null;
    /** What the user can do with a finished job, set by the job. */
    action?: JobAction | null;
    /** Whether a failed job may be queued again with its original arguments. */
    retryable?: boolean;
    /** The failed job this one retries. */
    retry_of?: number | null;
  }

  interface JobError {
    reason: string;
    message: string;
  }

  /**
   * A typed action a finished job offers. The frontend runs each kind through
   * the handler registered for it, so the drawer and the notification never
   * need to know which feature the job belongs to.
   */
  interface JobAction {
    kind: string;
    label: string;
    [key: string]: unknown;
  }

  /**
   * One configured media server destination. These are multi-instance, so the
   * status endpoint reports a list: two Embys are two entries, each named
   * after the instance. Only destinations actually in use appear at all.
   *
   * `state` is what is honestly known right now. "checking" means no probe has
   * answered yet, not that the server is down, and it resolves on a later
   * poll. "unreachable" carries no version rather than a stale one.
   */
  interface MediaServerStatus {
    id: string;
    kind: "emby" | "jellyfin" | "plex" | "silo";
    name: string;
    state: "connected" | "unreachable" | "checking";
    /** Empty when the server does not report one. Silo never does. */
    version: string;
    /**
     * Seconds this answer can stay as it is. Zero means a probe is running
     * now, so asking again shortly may return something else. Anything else
     * is time left on a cached value that cannot change before it expires,
     * which is how long the page may wait before asking again.
     *
     * Absent on responses from before the field existed, which reads as "no
     * idea", not as "ask again immediately".
     */
    refresh_in?: number;
  }

  interface Status {
    bazarr_config_directory: string;
    bazarr_directory: string;
    bazarr_version: string;
    database_engine: string;
    database_migration: string;
    operating_system: string;
    package_version: string;
    python_version: string;
    radarr_version: string;
    sportarr_version: string;
    sonarr_version: string;
    /**
     * Absent on responses from before media servers were reported, so every
     * reader must tolerate undefined rather than assume a list.
     */
    media_servers?: System.MediaServerStatus[];
    start_time: number;
    timezone: string;
    cpu_cores: number;
    compat_active: boolean;
  }

  interface Backups {
    type: string;
    filename: string;
    size: string;
    date: string;
    id: number;
  }

  interface Health {
    object: string;
    issue: string;
  }

  interface Provider {
    name: string;
    status: string;
    retry: string;
  }

  type LogType = "INFO" | "WARNING" | "ERROR" | "DEBUG";

  interface Log {
    type: System.LogType;
    timestamp: string;
    message: string;
    exception?: string;
  }
}
