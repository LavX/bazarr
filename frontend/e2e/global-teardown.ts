import { rm } from "node:fs/promises";
import { removeContainer } from "./lib/container";
import { readSharedRecord, runDir } from "./lib/shared";

/** Removes the shared instance, if a stateless spec started one. */
export default async function globalTeardown() {
  const record = await readSharedRecord();
  if (record) await removeContainer(record.name);
  await rm(runDir(), { recursive: true, force: true });
}
