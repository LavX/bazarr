import { FunctionComponent, PropsWithChildren, ReactNode } from "react";
import {
  faCircleInfo,
  faShieldHalved,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type {
  PluginBinary,
  PluginPermission,
  PluginPermissionMark,
  PluginSourceInfo,
} from "@/apis/raw/plugins";
import { permissionLabel } from "./format";
import styles from "./PluginConsent.module.scss";

/** One titled block of the dialog. */
export const Section: FunctionComponent<
  PropsWithChildren<{ title: string }>
> = ({ title, children }) => (
  <div className={styles.section} role="group" aria-label={title}>
    <h3 className={styles.sectionTitle}>{title}</h3>
    {children}
  </div>
);

/** Whether this host holds a plugin to a permission, in words and an icon, never in colour alone. */
export const Mark: FunctionComponent<{ mark: PluginPermissionMark }> = ({
  mark,
}) => (
  <span className={styles.mark} data-mark={mark}>
    <FontAwesomeIcon
      icon={mark === "enforced" ? faShieldHalved : faCircleInfo}
      aria-hidden="true"
    />
    {mark === "enforced" ? "Enforced" : "Declared only"}
  </span>
);

export const PermissionList: FunctionComponent<{
  permissions: PluginPermission[];
}> = ({ permissions }) =>
  permissions.length === 0 ? (
    <p className={styles.quiet}>It asks for no permissions.</p>
  ) : (
    <ul className={styles.lines}>
      {permissions.map((permission) => (
        <li key={permission.name} className={styles.line}>
          <span>{permissionLabel(permission.name)}</span>
          <span className={styles.code}>{permission.name}</span>
          <Mark mark={permission.mark} />
        </li>
      ))}
    </ul>
  );

/** The sentence under a permission list that says what the marks mean, only when one is declared. */
export const MarkNote: FunctionComponent<{
  permissions: PluginPermission[];
}> = ({ permissions }) =>
  permissions.some((permission) => permission.mark === "declared") ? (
    <p className={styles.quiet}>
      Enforced means this host holds the plugin to the permission. Declared only
      means the plugin says it needs it and nothing here confines it.
    </p>
  ) : null;

export const DestinationList: FunctionComponent<{ destinations: string[] }> = ({
  destinations,
}) => (
  <ul className={styles.lines}>
    {destinations.map((destination) => (
      <li key={destination} className={styles.code}>
        {destination}
      </li>
    ))}
  </ul>
);

/** What a program or container the plugin runs is, in words. */
export function binaryText(binary: PluginBinary): string {
  if (binary.artifact === "oci") {
    return "Runs a container image on this machine";
  }
  if (binary.artifact === "archive") {
    return "Runs a program it downloads, as part of its release";
  }
  return `Runs ${binary.artifact}`;
}

export const BinaryList: FunctionComponent<{ binaries: PluginBinary[] }> = ({
  binaries,
}) => (
  <ul className={styles.lines}>
    {binaries.map((binary) => (
      <li key={`${binary.artifact}-${binary.process}`}>
        {binaryText(binary)}{" "}
        <span className={styles.code}>({binary.process})</span>
      </li>
    ))}
  </ul>
);

/** Where a plugin comes from and whether Bazarr+ trusts that place, from Bazarr+'s own record of it. */
export function sourceText(source: PluginSourceInfo): string {
  if (source.development) {
    return "A development source. Its code has not been reviewed.";
  }
  if (source.official) {
    return "The official catalog, which Bazarr+ trusts.";
  }
  if (source.trusted) {
    return "A trusted catalog source.";
  }
  return "A community catalog source. Review it before you install.";
}

export const SourceLine: FunctionComponent<{
  source: PluginSourceInfo;
  lead?: ReactNode;
}> = ({ source, lead }) => (
  <p className={styles.line} style={{ margin: 0 }}>
    {lead}
    <strong>{source.name ?? source.id ?? "Unknown source"}</strong>
    <span className={styles.quiet}>{sourceText(source)}</span>
  </p>
);
