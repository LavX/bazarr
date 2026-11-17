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

const Cell: FunctionComponent<{ items: ReactNode[] }> = ({ items }) =>
  items.length === 0 ? (
    <span className={styles.quiet}>None</span>
  ) : (
    <ul className={styles.lines}>
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );

/** The update half of the dialog: the old release and the new one side by side, then what joins it. */
export const UpdateDelta: FunctionComponent<{
  delta: PluginUpdateDelta;
  before: InstalledFacts | null;
}> = ({ delta, before }) => {
  const { added } = delta;
  const newPermissions = added.permissions.map((item) => item.name);
  const afterPermissions = before
    ? [...before.permissions, ...newPermissions]
    : newPermissions;
  const afterDestinations = before
    ? [...before.destinations, ...added.destinations]
    : added.destinations;
  const newBinaries = added.binaries.map(binaryText);
  const beforeBinaries = before?.binaries.map(binaryText) ?? [];
  const mark = (name: string) =>
    newPermissions.includes(name) ? (
      <span className={styles.added}>{permissionLabel(name)} (new)</span>
    ) : (
      permissionLabel(name)
    );
  const newest = (value: string, fresh: boolean) =>
    fresh ? <span className={styles.added}>{value} (new)</span> : value;
  return (
    <>
      <Section title="What changes">
        <div className={styles.sideBySide}>
          <span />
          <span className={styles.columnHead}>Installed now</span>
          <span className={styles.columnHead}>After the update</span>

          <span className={styles.rowLabel}>Version</span>
          <span>{delta.plugin.from_version ?? "Unknown"}</span>
          <span className={styles.added}>{delta.plugin.to_version}</span>

          <span className={styles.rowLabel}>Permissions</span>
          <Cell
            items={(before?.permissions ?? []).map((name) =>
              permissionLabel(name),
            )}
          />
          <Cell items={afterPermissions.map(mark)} />

          <span className={styles.rowLabel}>Sends data to</span>
          <Cell items={before?.destinations ?? []} />
          <Cell
            items={afterDestinations.map((item) =>
              newest(item, added.destinations.includes(item)),
            )}
          />

          <span className={styles.rowLabel}>Runs</span>
          <Cell items={beforeBinaries} />
          <Cell
            items={[...beforeBinaries, ...newBinaries].map((item) =>
              newest(item, newBinaries.includes(item)),
            )}
          />
        </div>
        {before === null && (
          <p className={styles.quiet}>
            The installed release could not be read, so only what is new is
            listed in the second column.
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
          This update adds no new destination, permission, program or plugin.
        </p>
      )}
    </>
  );
};
