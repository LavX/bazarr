import { FunctionComponent } from "react";
import { Alert } from "@mantine/core";
import type {
  PluginConsentMember,
  PluginConsentSummary,
} from "@/apis/raw/plugins";
import { formatBytes, formatDollars, kindLabel } from "./format";
import {
  BinaryList,
  DestinationList,
  MarkNote,
  PermissionList,
  Section,
  SourceLine,
} from "./parts";
import styles from "./PluginConsent.module.scss";

/** What the person is told about spending before an AI engine is installed. */
export interface PluginCostCaps {
  /** Dollars, or null for no cap. */
  jobCap: number | null;
  monthCap: number | null;
  /** The one plugin the caps are about. Left out, they are shown for every install. */
  pluginId?: string;
}

const capText = (cap: number | null) =>
  cap === null ? "no cap set" : formatDollars(cap);

const MemberCard: FunctionComponent<{ member: PluginConsentMember }> = ({
  member,
}) => (
  <li className={styles.member}>
    <div className={styles.memberName}>
      {member.name} {member.version}{" "}
      <span className={styles.code}>{kindLabel(member.kind)}</span>
    </div>
    <SourceLine source={member.source} />
    <PermissionList permissions={member.permissions} />
    {member.destinations.length > 0 && (
      <DestinationList destinations={member.destinations} />
    )}
    {member.binaries.length > 0 && <BinaryList binaries={member.binaries} />}
  </li>
);

/** The install half of the dialog: everything the server's summary lists, nothing it does not. */
export const InstallSummary: FunctionComponent<{
  summary: PluginConsentSummary;
  costCaps?: PluginCostCaps;
}> = ({ summary, costCaps }) => {
  const root = summary.members.find(
    (member) => member.id === summary.plugin.id,
  );
  const others = summary.members.filter(
    (member) => member.id !== summary.plugin.id,
  );
  const hasNetwork = summary.permissions.some(
    (permission) => permission.name === "network",
  );
  const hasDocker = summary.permissions.some(
    (permission) => permission.name === "docker",
  );
  return (
    <>
      <Section title="Where it comes from">
        {root ? (
          <SourceLine source={root.source} />
        ) : (
          <p className={styles.quiet}>The catalog did not name a source.</p>
        )}
      </Section>
      <Section title="Download size">
        <p className={styles.lines}>
          {summary.download_size === null
            ? "The catalog does not state it."
            : formatBytes(summary.download_size)}
        </p>
      </Section>
      <Section title="What it may do">
        <PermissionList permissions={summary.permissions} />
        <MarkNote permissions={summary.permissions} />
      </Section>
      <Section title="What leaves this machine">
        {summary.destinations.length > 0 ? (
          <>
            <p className={styles.quiet}>
              What it works on, and any key you give it, can be sent to:
            </p>
            <DestinationList destinations={summary.destinations} />
          </>
        ) : hasNetwork ? (
          <p className={styles.quiet}>
            It can reach the network, and the plugin does not fix which
            addresses.
          </p>
        ) : (
          <p className={styles.quiet}>Nothing is sent from this machine.</p>
        )}
      </Section>
      <Section title="Programs and containers">
        {summary.binaries.length > 0 ? (
          <BinaryList binaries={summary.binaries} />
        ) : (
          <p className={styles.quiet}>It runs no program or container.</p>
        )}
        {hasDocker && (
          <Alert color="yellow" role="note" variant="light" p="xs">
            It needs access to the Docker socket. That is root-equivalent on
            this machine.
          </Alert>
        )}
      </Section>
      {others.length > 0 && (
        <Section title="Also installed with it">
          <ul className={styles.lines}>
            {others.map((member) => (
              <MemberCard key={member.id} member={member} />
            ))}
          </ul>
        </Section>
      )}
      {summary.already_installed.length > 0 && (
        <Section title="Already installed">
          <ul className={styles.lines}>
            {summary.already_installed.map((item, index) => (
              <li key={`${item.id ?? index}-${item.capability ?? ""}`}>
                {item.id}
                {item.capability && (
                  <span className={styles.code}> ({item.capability})</span>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {summary.external_requirements.length > 0 && (
        <Section title="Needs something this install does not supply">
          <ul className={styles.lines}>
            {summary.external_requirements.map((item) => (
              <li key={item} className={styles.code}>
                {item}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {summary.memory_request !== null && (
        <Section title="Memory">
          <p className={styles.quiet}>
            It asks for up to {formatBytes(summary.memory_request)}, more than
            this kind of plugin gets by default.
          </p>
        </Section>
      )}
      {costCaps &&
        (!costCaps.pluginId || costCaps.pluginId === summary.plugin.id) && (
          <Section title="Spending">
            <p className={styles.quiet}>
              Cost caps right now: {capText(costCaps.jobCap)} per job,{" "}
              {capText(costCaps.monthCap)} per month.
            </p>
          </Section>
        )}
      <Section title="Removing it">
        <p className={styles.quiet}>
          You can remove it at any time from the Plugin Hub. Its settings and
          data are kept unless you choose to delete them.
        </p>
      </Section>
    </>
  );
};
