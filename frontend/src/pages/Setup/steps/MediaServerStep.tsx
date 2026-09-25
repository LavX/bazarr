import { FC, useMemo } from "react";
import { Button, Checkbox, Group, Text } from "@mantine/core";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import { useMediaServerInstances } from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { kindName } from "@/pages/Settings/MediaServers/kinds";
import { readMediaServerTest } from "@/pages/Setup/connectionTests";
import StepLayout from "@/pages/Setup/StepLayout";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import ConnectedServerRow from "./mediaServer/ConnectedServerRow";
import StepActions from "./mediaServer/StepActions";
import type { WizardStepProps } from "./types";
import styles from "./MediaServerStep.module.scss";

// The same four kinds, in the same order, as the Connections tabs.
const DESCRIPTIONS: Record<MediaServerKind, string> = {
  plex: "Sign in with your Plex account and pick the server to refresh.",
  jellyfin: "Connect with the server URL and an API key.",
  emby: "Connect with the server URL and an API key, then map your media folders.",
  silo: "Connect with the server URL and an API key, then map your media folders to a library.",
};
const ORDER: MediaServerKind[] = ["plex", "jellyfin", "emby", "silo"];

/**
 * Optional onboarding step for external media servers. It picks which servers
 * to connect and nothing else: each one is configured on a step of its own,
 * generated from what is ticked here.
 *
 * Several at once, including two of one kind, because that is what the backend
 * has always stored: media_server_instances has no uniqueness on name or kind,
 * and Settings already lists as many per kind as exist. The single-choice
 * picker was the only thing saying otherwise.
 *
 * Plex is the exception, and it is one entry. Its connection is not a form but
 * the account panel, and the account is a singleton: one token, one chosen
 * server, one destination row the backend keeps in step with it. A second Plex
 * entry here would be a second screen driving that same row, so a second server
 * selection would overwrite the first rather than connect anything. A reader who
 * really runs two Plex servers adds the second in Settings, Connections, where
 * there is a form to give it its own address.
 *
 * Every kind stays optional and Continue with nothing ticked writes nothing,
 * because Bazarr+ finds and downloads subtitles with no media server at all.
 * Skipping is the shell's control.
 */
const MediaServerStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { drafts, addDraft, removeKind, removeDraft } =
    useOnboardingSelection();

  const { data: settings, isPending: settingsPending } = useSystemSettings();
  const settingsMutation = useSettingsMutation();
  // One query per kind, a fixed four, so no hook is called in a loop.
  const plex = useMediaServerInstances("plex");
  const jellyfin = useMediaServerInstances("jellyfin");
  const emby = useMediaServerInstances("emby");
  const silo = useMediaServerInstances("silo");

  const connected = useMemo<Record<MediaServerKind, MediaServerInstance[]>>(
    () => ({
      plex: plex.data ?? [],
      jellyfin: jellyfin.data ?? [],
      emby: emby.data ?? [],
      silo: silo.data ?? [],
    }),
    [plex.data, jellyfin.data, emby.data, silo.data],
  );

  // Which Plex row the account owns, by the same rule the backend applies
  // (media_servers/plex_account.py::_account_row): the recorded id and nothing
  // else. A sign-in never adopts a row it did not create, so an id that matches
  // no row means the account owns none, not that it owns whichever row is
  // first. Disconnecting the owned row has to sign the account out; deleting it
  // on its own leaves the token and the chosen server behind for the next
  // reconcile to rebuild it from.
  const plexAccountRowId = useMemo(() => {
    const rows = plex.data ?? [];
    // Until the settings answer, the recorded id is unknown rather than empty,
    // and treating unknown as "owns nothing" would delete the account's own row
    // without signing out, which the next reconcile rebuilds from the token
    // left behind. So ownership stays unknown until it is known, and the row
    // says so by not offering to disconnect yet.
    if (rows.length === 0 || settingsPending) {
      return null;
    }
    const recorded = settings?.plex?.instance_id ?? "";
    return rows.find((row) => row.id === recorded)?.id ?? null;
  }, [plex.data, settings?.plex?.instance_id, settingsPending]);

  const takenNames = useMemo(
    () => ORDER.flatMap((kind) => connected[kind]).map((row) => row.name),
    [connected],
  );

  const pendingOf = (kind: MediaServerKind) =>
    drafts.filter(
      (draft) => draft.kind === kind && draft.instanceId === undefined,
    );

  const toggle = (kind: MediaServerKind, checked: boolean) => {
    if (checked) {
      addDraft(kind, takenNames);
    } else {
      removeKind(kind);
    }
  };

  const forgetInstance = (instanceId: string) => {
    // A draft that has written its row but is still on screen, because its
    // master switch failed, holds that row under savedInstanceId rather than
    // instanceId. Matching only the latter left such a draft standing after
    // the reader disconnected its row from here, and continuing from its step
    // then marked a deleted id as saved without writing anything at all.
    const draft = drafts.find(
      (entry) =>
        entry.instanceId === instanceId || entry.savedInstanceId === instanceId,
    );
    if (draft) {
      removeDraft(draft.draftId);
    }
  };

  const pendingTotal = drafts.filter(
    (draft) => draft.instanceId === undefined,
  ).length;

  // Whether the kind's master switch is on. The dispatcher reads it before it
  // reads any row, so an enabled instance under a switched-off kind refreshes
  // nothing; calling that connected is the same lie the Finish recap used to
  // tell. Unknown until the settings answer, and unknown is not off.
  const switchedOn = (kind: MediaServerKind) =>
    settings?.general === undefined ||
    (settings.general as LooseObject)[`use_${kind}`] === true;

  // What the reader has lined up for one kind, in the card's own words. Four
  // numbers, because they mean different things: a connected server is a row
  // that refreshes and passed its Test, a saved one refreshes but no Test has
  // passed against it, a stalled one is a row the switch is keeping quiet, and
  // a pending one is a tick that has written nothing yet.
  const summarise = (
    live: number,
    saved: number,
    stalled: number,
    waiting: number,
  ) => {
    const parts: string[] = [];
    if (live > 0) {
      parts.push(`${live} connected`);
    }
    if (saved > 0) {
      parts.push(`${saved} saved`);
    }
    if (stalled > 0) {
      parts.push(`${stalled} not refreshing`);
    }
    if (waiting > 0) {
      parts.push(`${waiting} to set up`);
    }
    return parts.join(", ");
  };

  return (
    <StepLayout
      title="Media servers"
      description="Optionally connect a media server so Bazarr refreshes it after it downloads subtitles. Bazarr finds and downloads subtitles with no media server at all, so you can set one up later in Settings, Connections."
      layout="wide"
      actions={
        <StepActions
          onNext={onNext}
          onBack={onBack}
          continueLabel={
            pendingTotal > 0
              ? `Set up ${pendingTotal} ${pendingTotal === 1 ? "server" : "servers"}`
              : "Continue without a server"
          }
        />
      }
    >
      <div className={styles.kinds}>
        {ORDER.map((kind) => {
          const rows = connected[kind];
          // A switched-off row is not a destination: the dispatcher skips it,
          // so counting it as connected would tick this card, lock it, and
          // leave the reader no way to set that kind up at all. The one the
          // backend keeps after a Plex sign-out is exactly that row, and it
          // has had its credential cleared as well. It is still listed below,
          // saying what it is, because it is still a row to disconnect.
          const enabledRows = rows.filter((row) => row.enabled);
          const kindOn = switchedOn(kind);
          const live = kindOn ? enabledRows : [];
          const stalled = kindOn ? [] : enabledRows;
          const pending = pendingOf(kind);
          // Plex is the kind whose card is not answered by "a Plex row
          // exists". A row somebody added by hand in Connections is not the
          // account, and this card is the only way to reach the account
          // sign-in: there is no "Add another Plex" beside it, so locking it
          // over a hand-added sibling left signing in impossible without
          // deleting a server the reader never asked about. Until the settings
          // say which row the account owns, any row stands in for it, so the
          // card does not tick and untick itself while that query lands.
          const answered =
            kind === "plex" && !settingsPending
              ? enabledRows.some((row) => row.id === plexAccountRowId)
              : enabledRows.length > 0;
          const checked = answered || pending.length > 0;
          const locked = answered && pending.length === 0;
          const tested = live.filter(
            (row) => readMediaServerTest(kind, row.id) === "passed",
          ).length;
          const summary = summarise(
            tested,
            live.length - tested,
            stalled.length,
            pending.length,
          );
          return (
            <div
              key={kind}
              className={[
                styles.kindCard,
                checked ? styles.kindCardChecked : "",
                locked ? styles.kindCardLocked : "",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              <Checkbox.Card
                className={styles.card}
                // The wrapper carries the border, so the footer below can hold
                // controls of its own without nesting them inside a button.
                withBorder={false}
                radius="md"
                checked={checked}
                // A connected server is undone by disconnecting it, not by
                // unticking it: the row is already written.
                disabled={locked}
                aria-label={kindName(kind)}
                onClick={() => (locked ? undefined : toggle(kind, !checked))}
              >
                <div className={styles.cardBody}>
                  <Checkbox.Indicator />
                  <div className={styles.cardText}>
                    <Text fw={500}>{kindName(kind)}</Text>
                    <Text size="sm" c="dimmed">
                      {DESCRIPTIONS[kind]}
                    </Text>
                  </div>
                </div>
              </Checkbox.Card>

              {(rows.length > 0 || pending.length > 0) && (
                <div className={styles.footer}>
                  {rows.map((instance) => (
                    <ConnectedServerRow
                      key={instance.id}
                      instance={instance}
                      kind={kind}
                      last={rows.length === 1}
                      accountOwned={
                        kind === "plex" && instance.id === plexAccountRowId
                      }
                      ownershipPending={kind === "plex" && settingsPending}
                      kindEnabled={kindOn}
                      onDisconnected={forgetInstance}
                    />
                  ))}
                  {/* One pending server is undone by unticking the card, so it
                      needs no control of its own. Several cannot be: unticking
                      would drop them all, and the reader who added a second
                      Emby has to be able to drop just that one. */}
                  {pending.length > 1 &&
                    pending.map((draft) => (
                      <Group
                        key={draft.draftId}
                        justify="space-between"
                        gap="sm"
                      >
                        <Text size="sm">{draft.name}</Text>
                        <Button
                          variant="subtle"
                          color="gray"
                          size="compact-sm"
                          aria-label={`Remove ${draft.name}`}
                          onClick={() => removeDraft(draft.draftId)}
                        >
                          Remove
                        </Button>
                      </Group>
                    ))}
                  <div className={styles.footerRow}>
                    <Text size="sm" c="dimmed">
                      {summary}
                    </Text>
                    {/* A row that exists under a switched-off kind is undone
                        by turning the kind back on, not by adding another
                        server or deleting this one. Without this the picker
                        offered neither, and the reader's only honest move was
                        to finish setup and find Settings, Connections. */}
                    {stalled.length > 0 && (
                      <Button
                        variant="light"
                        size="compact-sm"
                        loading={settingsMutation.isPending}
                        onClick={() =>
                          settingsMutation.mutate({
                            [`settings-general-use_${kind}`]: true,
                          })
                        }
                      >
                        Turn on {kindName(kind)} refreshes
                      </Button>
                    )}
                    {kind === "plex" ? (
                      <Text size="sm" c="dimmed">
                        One Plex account per install.
                      </Text>
                    ) : (
                      <Button
                        variant="subtle"
                        size="compact-sm"
                        onClick={() => addDraft(kind, takenNames)}
                      >
                        Add another {kindName(kind)}
                      </Button>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </StepLayout>
  );
};

export default MediaServerStep;
