import { FunctionComponent, ReactNode, useState } from "react";
import { useId } from "@mantine/hooks";
import {
  faChevronDown,
  faChevronUp,
  faCircleInfo,
  faFileSignature,
  faListCheck,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemStatus } from "@/apis/hooks";
import type {
  PluginCatalogEntry,
  PluginCatalogSource,
  PluginPermission,
  PluginSourceInfo,
} from "@/apis/raw/plugins";
import type { ProviderHubCatalogEntry } from "@/apis/raw/providerHub";
import { capabilityLabel } from "@/components/hub/CapabilityStatus";
import { StatusBadge } from "@/components/hub/StatusBadge";
import { parseManifest } from "@/components/hub/utils";
import {
  BinaryList,
  DestinationList,
  MarkNote,
  PermissionList,
  SourceLine,
} from "@/components/plugin-consent/parts";
import {
  manifestFacts,
  permissionMark,
  sandboxNote,
} from "@/pages/PluginHub/model";
import styles from "./SchemaTwoDetails.module.scss";
import consent from "@/components/plugin-consent/PluginConsent.module.scss";

/** Whether a catalog entry's manifest is schema 2, the format that declares permissions and destinations. */
export function isSchemaTwo(
  entry: ProviderHubCatalogEntry | null | undefined,
): boolean {
  return parseManifest(entry)?.schema_version === 2;
}

export type SignatureState = "signed" | "unsigned" | "not-signed";

/**
 * What the signature check of the entry's catalog came to, from Bazarr+'s own record of the source. Only
 * the official catalog is checked, so another source is "not-signed", never unsigned by a failed check.
 * Null for a local package, which is from no catalog, and while the source record is not known.
 */
export function signatureState(
  source: PluginCatalogSource | undefined,
  local: boolean,
): SignatureState | null {
  if (local || !source) {
    return null;
  }
  if (source.signature_verified === true) {
    return "signed";
  }
  return source.catalog_signature ? "unsigned" : "not-signed";
}

const SIGNATURE_TEXT: Record<SignatureState, string> = {
  signed: "Its catalog is signed, and Bazarr+ checked the signature.",
  unsigned: "Its catalog is not signed. This is the older catalog format.",
  "not-signed":
    "Its catalog is not signed. Bazarr+ checks signatures only on its official catalog.",
};

/** The signature of the catalog, as a pill with text and an icon, for the footer of a card. */
export const SignatureBadge: FunctionComponent<{
  state: SignatureState | null;
}> = ({ state }) => {
  if (!state) {
    return null;
  }
  return state === "signed" ? (
    <StatusBadge
      tone="info"
      label="Signed catalog"
      icon={faFileSignature}
      variant="icon"
    />
  ) : (
    <StatusBadge
      tone="neutral"
      label="Unsigned catalog"
      icon={faCircleInfo}
      variant="icon"
    />
  );
};

const count = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`;

/** "declared only" when this host confines none of them, "enforced" when it confines all, the split otherwise. */
function markSummary(permissions: PluginPermission[]): string {
  const enforced = permissions.filter(
    (item) => item.mark === "enforced",
  ).length;
  if (enforced === 0) {
    return "declared only";
  }
  if (enforced === permissions.length) {
    return "enforced";
  }
  return `${enforced} enforced, ${permissions.length - enforced} declared only`;
}

interface SchemaTwoDetailsProps {
  entry: ProviderHubCatalogEntry;
  name: string;
  /** The catalog source record the entry comes from, as Bazarr+ keeps it. */
  source?: PluginCatalogSource;
  /** True for a package uploaded by hand, which belongs to no catalog. */
  local?: boolean;
}

const Group: FunctionComponent<{
  title: string;
  children: ReactNode;
}> = ({ title, children }) => (
  <div className={styles.group} role="group" aria-label={title}>
    <div className={styles.groupTitle}>{title}</div>
    {children}
  </div>
);

/**
 * What a schema 2 provider declares, for the person to judge before installing: one line on the card, and
 * the permissions, destinations, capabilities, trust and signature behind it in an area that opens. The
 * permission and destination lists, their marks and their wording are the Plugin Hub's own.
 */
export const SchemaTwoDetails: FunctionComponent<SchemaTwoDetailsProps> = ({
  entry,
  name,
  source,
  local = false,
}) => {
  const [open, setOpen] = useState(false);
  const regionId = useId();
  const status = useSystemStatus();
  const sandbox = status.data?.sandbox;
  const facts = manifestFacts(entry);
  const permissions: PluginPermission[] = facts.permissions.map((item) => ({
    name: item,
    mark: permissionMark(item, sandbox),
  }));
  const hasNetwork = facts.permissions.includes("network");
  const signature = signatureState(source, local);
  const sandboxLine = sandboxNote(facts.permissions, sandbox);

  const provider: PluginSourceInfo = {
    id: entry.source ?? null,
    name: entry.source_name ?? entry.source ?? null,
    official: source?.official === true,
    trusted: Boolean(entry.trusted),
    development: (entry as PluginCatalogEntry).development_source === true,
  };

  const summary = [
    permissions.length === 0
      ? "no permissions"
      : `${count(permissions.length, "permission")} (${markSummary(permissions)})`,
    facts.endpoints.length > 0
      ? count(facts.endpoints.length, "destination")
      : hasNetwork
        ? "no fixed destination"
        : null,
    facts.requires.length > 0
      ? `needs ${count(facts.requires.length, "capability", "capabilities")}`
      : null,
    facts.provides.length > 0
      ? `supplies ${count(facts.provides.length, "capability", "capabilities")}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const capabilityRows: Array<[string, string]> = [
    ...facts.requires.map((id): [string, string] => ["Needs", id]),
    ...facts.provides.map((id): [string, string] => ["Supplies", id]),
    ...(facts.extendsId
      ? ([
          [
            "Extends",
            facts.extendsCapability
              ? `${facts.extendsId} (${facts.extendsCapability})`
              : facts.extendsId,
          ],
        ] as Array<[string, string]>)
      : []),
  ];

  return (
    <div className={styles.root}>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        aria-controls={open ? regionId : undefined}
        onClick={() => setOpen((value) => !value)}
      >
        <FontAwesomeIcon
          icon={faListCheck}
          className={styles.toggleIcon}
          aria-hidden="true"
        />
        <span className={styles.toggleText}>
          <span className={styles.lead}>Declares</span> {summary}
        </span>
        <FontAwesomeIcon
          icon={open ? faChevronUp : faChevronDown}
          className={styles.chevron}
          aria-hidden="true"
        />
      </button>
      {open && (
        <div
          id={regionId}
          role="region"
          aria-label={`What ${name} declares`}
          className={styles.panel}
        >
          <Group title="Where it comes from">
            {local ? (
              <p className={styles.text}>
                An uploaded package. It belongs to no catalog, so Bazarr+ does
                not trust it.
              </p>
            ) : (
              <SourceLine source={provider} />
            )}
            {signature && (
              <p className={styles.text}>{SIGNATURE_TEXT[signature]}</p>
            )}
          </Group>
          <Group title="What it may do">
            <PermissionList permissions={permissions} />
            <MarkNote permissions={permissions} />
            {sandboxLine && (
              <p className={styles.note} role="note">
                <FontAwesomeIcon icon={faCircleInfo} aria-hidden="true" />
                {sandboxLine}
              </p>
            )}
          </Group>
          <Group title="What leaves this machine">
            {facts.endpoints.length > 0 ? (
              <>
                <p className={consent.quiet}>
                  What it works on, and any key you give it, can be sent to:
                </p>
                <DestinationList destinations={facts.endpoints} />
              </>
            ) : hasNetwork ? (
              <p className={consent.quiet}>
                It can reach the network, and the plugin does not fix which
                addresses.
              </p>
            ) : (
              <p className={consent.quiet}>
                Nothing is sent from this machine.
              </p>
            )}
            {facts.hasDestinationField && (
              <p className={consent.quiet}>
                It also connects to an address you enter in its settings.
              </p>
            )}
          </Group>
          {facts.binaries.length > 0 && (
            <Group title="Programs and containers">
              <BinaryList binaries={facts.binaries} />
            </Group>
          )}
          {capabilityRows.length > 0 && (
            <Group title="Capabilities">
              <ul className={consent.lines}>
                {capabilityRows.map(([role, id]) => (
                  <li key={`${role}-${id}`} className={styles.capabilityRow}>
                    <span className={styles.capabilityRole}>{role}</span>
                    <span>{capabilityLabel(id)}</span>
                    {capabilityLabel(id) !== id && (
                      <span className={consent.code}>{id}</span>
                    )}
                  </li>
                ))}
              </ul>
            </Group>
          )}
        </div>
      )}
    </div>
  );
};
