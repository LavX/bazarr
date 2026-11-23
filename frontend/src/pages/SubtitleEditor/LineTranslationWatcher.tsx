import { useEffect } from "react";
import {
  isTerminalJob,
  readEditorTranslation,
  useEditorJob,
} from "./editorJobs";

export interface LineTranslationJob {
  jobId: number;
  /** The backend run the job was queued under. */
  session: string | undefined;
  /** The cue the translated line lands on. */
  cueId: string;
}

/**
 * Follows one queued single-line translation and hands its line to the editor
 * when it ends. One of these runs for every line in flight, so starting a
 * second line while the first is still translating does not drop the first.
 * It renders nothing.
 */
export default function LineTranslationWatcher({
  job,
  onLine,
  onDone,
}: {
  job: LineTranslationJob;
  onLine: (cueId: string, line: string) => void;
  onDone: (jobId: number) => void;
}) {
  const followed = useEditorJob(job.jobId, job.session);
  const finished = isTerminalJob(followed);
  const { jobId, cueId } = job;
  useEffect(() => {
    if (!finished) return;
    let active = true;
    readEditorTranslation(jobId)
      .then((state) => {
        const line = state.status === "completed" ? state.lines?.[0]?.line : "";
        if (active && line) onLine(cueId, line);
      })
      .catch(() => undefined)
      .finally(() => {
        if (active) onDone(jobId);
      });
    return () => {
      active = false;
    };
  }, [finished, jobId, cueId, onLine, onDone]);
  return null;
}
