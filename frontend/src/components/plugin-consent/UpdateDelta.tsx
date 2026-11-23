import { FunctionComponent, ReactNode } from "react";
import type {
  PluginBinary,
  PluginInstallation,
  PluginUpdateDelta,
} from "@/apis/raw/plugins";
import { permissionLabel } from "./format";
import { binaryText, MarkNote, PermissionList, Section } from "./parts";
import styles from "./PluginConsent.module.scss";

/** What the installed release declares, read from its stored manifest. */
export interface InstalledFacts {
  permissions: string[];
  destinations: string[];
  binaries: PluginBinary[];
}

const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

/** The permissions, fixed origins and program the installed release declares, or null when it cannot be read. */
export function installedFacts(
  installation: PluginInstallation | undefined,
): InstalledFacts | null {
  const manifest = installation?.manifest as
    | Record<string, unknown>
    | undefined;
  if (!manifest || typeof manifest !== "object") {
    return null;
  }
  const destinations = new Set(strings(manifest.endpoints));
  const settings = manifest.settings as
    | { backend_types?: Record<string, { endpoints?: unknown }> }
    | undefined;
  for (const definition of Object.values(settings?.backend_types ?? {})) {
    strings(definition?.endpoints).forEach((item) => destinations.add(item));
  }
  const runtime = manifest.runtime as
    | { artifact?: string; process?: string }
    | undefined;
  const binaries: PluginBinary[] =
    runtime?.artifact === "archive" || runtime?.artifact === "oci"
      ? [{ artifact: runtime.artifact, process: runtime.process ?? "" }]
      : [];
  return {
    permissions: strings(manifest.permissions),
    destinations: [...destinations].sort(),
    binaries,
  };
}

const Cell: FunctionComponent<{ items: ReactNode[]; none?: string }> = ({
  items,
  none = "None",
}) =>
  items.length === 0 ? (
    <span className={styles.quiet}>{none}</span>
  ) : (
    <ul className={styles.lines}>
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );

/**
 * The update half of the dialog: what the installed release declares beside what the update adds to it.
 * The server's delta lists only additions, so the second column says exactly that and never guesses
 * at what the new release drops.
 */
export const UpdateDelta: FunctionComponent<{
  delta: PluginUpdateDelta;
  before: InstalledFacts | null;
  action?: "update" | "roll back";
}> = ({ delta, before, action = "update" }) => {
  const { added } = delta;
  const beforeBinaries = before?.binaries.map(binaryText) ?? [];
  return (
    <>
      <Section title="What changes">
        <div className={styles.sideBySide}>
          <span className={styles.corner} />
          <span className={styles.columnHead}>Installed now</span>
          <span className={styles.columnHead}>This {action} adds</span>

          <span className={styles.rowLabel}>Version</span>
          <span>{delta.plugin.from_version ?? "Unknown"}</span>
          <span className={styles.added}>{delta.plugin.to_version}</span>

          <span className={styles.rowLabel}>Permissions</span>
          <Cell
            items={(before?.permissions ?? []).map((name) =>
              permissionLabel(name),
            )}
          />
          <Cell
            none="Nothing new"
            items={added.permissions.map((item) => permissionLabel(item.name))}
          />

          <span className={styles.rowLabel}>Sends data to</span>
          <Cell items={before?.destinations ?? []} />
          <Cell none="Nothing new" items={added.destinations} />

          <span className={styles.rowLabel}>Runs</span>
          <Cell items={beforeBinaries} />
          <Cell none="Nothing new" items={added.binaries.map(binaryText)} />
        </div>
        {before === null && (
          <p className={styles.quiet}>
            The installed release could not be read, so only what the {action}{" "}
            adds is listed.
          </p>
        )}
      </Section>
      {added.destination_fields.length > 0 && (
        <Section title="New address settings">
          <p className={styles.quiet}>
            You will be able to point it at an address you type, in these
            settings:
          </p>
          <ul className={styles.lines}>
            {added.destination_fields.map((field) => (
              <li key={field} className={styles.code}>
                {field}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {added.permissions.length > 0 && (
        <Section title="New permissions">
          <PermissionList permissions={added.permissions} />
          <MarkNote permissions={added.permissions} />
        </Section>
      )}
      {added.plugins.length > 0 && (
        <Section title="Plugins it adds">
          <ul className={styles.lines}>
            {added.plugins.map((plugin) => (
              <li key={plugin.id} className={styles.member}>
                <div className={styles.memberName}>
                  {plugin.name} {plugin.version}
                </div>
                <PermissionList permissions={plugin.permissions} />
                {plugin.destinations.length > 0 && (
                  <ul className={styles.lines}>
                    {plugin.destinations.map((destination) => (
                      <li key={destination} className={styles.code}>
                        {destination}
                      </li>
                    ))}
                  </ul>
                )}
                {plugin.binaries.map((binary) => (
                  <span key={binary.artifact}>{binaryText(binary)}</span>
                ))}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {!delta.needs_consent && (
        <p className={styles.quiet}>
          This {action} adds no new destination, permission, program or plugin.
        </p>
      )}
    </>
  );
};
