import { FunctionComponent, ReactNode } from "react";
import { Button, Stack } from "@mantine/core";
import {
  faListCheck,
  faPlug,
  faRotate,
  faStore,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { EmptyState } from "@/components/hub/EmptyState";
import { KIND_PURPOSE, PluginRow, TypeFilter } from "./model";
import { PluginCard } from "./PluginCard";
import styles from "@/components/hub/hub.module.scss";

export interface CardActions {
  sandbox: Parameters<typeof PluginCard>[0]["sandbox"];
  onInstall: (row: PluginRow) => void;
  onUpdate: (row: PluginRow) => void;
  updating: string | null;
}

const Grid: FunctionComponent<{ rows: PluginRow[] } & CardActions> = ({
  rows,
  sandbox,
  onInstall,
  onUpdate,
  updating,
}) => (
  <div className={styles.cardGrid}>
    {rows.map((row) => (
      <PluginCard
        key={`${row.key}-${row.version}`}
        row={row}
        sandbox={sandbox}
        onInstall={onInstall}
        onUpdate={onUpdate}
        updating={updating === row.id}
        blocked={updating !== null && updating !== row.id}
      />
    ))}
  </div>
);

const Header: FunctionComponent<{
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}> = ({ eyebrow, title, description, action }) => (
  <div className={styles.panelHeader}>
    <div>
      <div className={styles.eyebrow}>{eyebrow}</div>
      <h2 className={styles.panelTitle}>{title}</h2>
      <p className={styles.panelDescription}>{description}</p>
    </div>
    {action}
  </div>
);

interface PanelProps extends CardActions {
  rows: PluginRow[];
  filter: TypeFilter;
  /** Something is typed in the search box. */
  searching: boolean;
}

export const MarketplacePanel: FunctionComponent<PanelProps> = ({
  rows,
  filter,
  searching,
  ...actions
}) => (
  <Stack gap="md">
    <Header
      eyebrow="Marketplace"
      title="Browse the catalog"
      description="Translators, translator back ends and services from the official catalog. Subtitle providers are in the Subtitle Hub."
    />
    {rows.length > 0 ? (
      <Grid rows={rows} {...actions} />
    ) : (
      <EmptyState
        icon={faStore}
        title={
          searching ? "No matching plugins" : "Nothing in the catalog for this"
        }
        body={
          searching
            ? "Try a different search term or another type."
            : KIND_PURPOSE[filter]
        }
      />
    )}
  </Stack>
);

export const InstalledPanel: FunctionComponent<
  PanelProps & { onBrowse: () => void }
> = ({ rows, filter, searching, onBrowse, ...actions }) => (
  <Stack gap="md">
    <Header
      eyebrow="Installed"
      title="Your plugins"
      description="What is installed, where it came from and what it may do."
    />
    {rows.length > 0 ? (
      <Grid rows={rows} {...actions} />
    ) : (
      <EmptyState
        icon={faPlug}
        title={searching ? "No matching plugins" : "Nothing installed yet"}
        body={
          searching
            ? "Try a different search term or another type."
            : KIND_PURPOSE[filter]
        }
        action={
          searching ? undefined : (
            <Button variant="light" onClick={onBrowse}>
              Browse the Marketplace
            </Button>
          )
        }
      />
    )}
  </Stack>
);

export const UpdatesPanel: FunctionComponent<
  Omit<PanelProps, "searching"> & {
    onCheck: () => void;
    checking: boolean;
    /** Updates there are for plugins of the other types, which the filter hides. */
    elsewhere: number;
  }
> = ({ rows, filter, onCheck, checking, elsewhere, ...actions }) => (
  <Stack gap="md">
    <Header
      eyebrow="Updates"
      title="Updates for your plugins"
      description="An update that adds a destination, a permission, a program or a plugin asks you again first. An ordinary one does not."
      action={
        <Button
          variant="default"
          leftSection={<FontAwesomeIcon icon={faRotate} />}
          loading={checking}
          onClick={onCheck}
        >
          Check for updates
        </Button>
      }
    />
    {rows.length > 0 ? (
      <Grid rows={rows} {...actions} />
    ) : (
      <EmptyState
        icon={faListCheck}
        title={
          filter === "all" || elsewhere === 0
            ? "Everything is up to date"
            : "No updates for this type"
        }
        body={
          elsewhere > 0 && filter !== "all"
            ? `${elsewhere} update${elsewhere === 1 ? " is" : "s are"} waiting for other types. Choose All to see ${elsewhere === 1 ? "it" : "them"}.`
            : "When the catalog lists a newer release of a plugin you have, it shows here."
        }
      />
    )}
  </Stack>
);
