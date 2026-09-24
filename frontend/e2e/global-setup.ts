import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** Gives the run a private scratch directory the workers can coordinate in. */
export default async function globalSetup() {
  process.env.BAZARR_E2E_RUN_DIR = await mkdtemp(join(tmpdir(), "bazarr-e2e-"));
}
