import { rm } from "node:fs/promises";
import { removeContainer, removeRunLeftovers } from "./lib/container";
import { readSharedRecord, runDir } from "./lib/shared";

/**
 * Removes the shared instance, if a stateless spec started one, and then
 * anything else labelled with this run: the container of a worker that was
 * killed, or of a start that failed before it could clean up.
 */
export default async function globalTeardown() {
  const record = await readSharedRecord();
  if (record) await removeContainer(record.name);
  if (!process.env.BAZARR_E2E_URL) await removeRunLeftovers();
  await rm(runDir(), { recursive: true, force: true });
}
