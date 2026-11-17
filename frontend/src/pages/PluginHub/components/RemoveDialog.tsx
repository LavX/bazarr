import { FunctionComponent, useEffect, useRef, useState } from "react";
import { Alert, Button, Group, Modal, Radio, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type { PluginRow } from "@/pages/PluginHub/model";
import type { TranslationState } from "@/types/translation";
import styles from "@/pages/PluginHub/PluginHub.module.scss";

/**
 * What removing a plugin does to the translation engines, from the Translation API: the engine the plugin
 * is, and an engine whose last enabled connection goes through the plugin. With the data kept the engine
 * stays on the page with its settings, and with it deleted the engine goes. Empty when the server does not
 * run the new translator or cannot say.
 */
export function engineEffects(
  state: TranslationState | null | undefined,
  pluginId: string,
  keepData: boolean,
): string[] {
  const effects: string[] = [];
  for (const engine of state?.instances ?? []) {
    if (engine.plugin_id === pluginId) {
      effects.push(
        keepData
          ? `${engine.name} is an engine on the Translation page. It stays there with its settings and connections, and cannot translate until the plugin is installed again.`
          : `${engine.name} is an engine on the Translation page. Removing the plugin removes the engine and its connections.`,
      );
      continue;
    }
    const served = engine.connections.filter(
      (item) => item.plugin_id === pluginId,
    );
    if (served.length > 0) {
      const these = served.length === 1 ? "connection goes" : "connections go";
      effects.push(
        keepData
          ? `${served.length} ${these} through this plugin on ${engine.name}. ${served.length === 1 ? "It stays" : "They stay"}, and cannot be used until the plugin is installed again.`
          : `${served.length} ${these} through this plugin on ${engine.name}. ${served.length === 1 ? "It is" : "They are"} deleted with the plugin, keys included.`,
      );
    }
    const enabled = engine.connections.filter((item) => item.enabled);
    const left = enabled.filter((item) => item.plugin_id !== pluginId);
    if (enabled.length > 0 && left.length === 0) {
      effects.push(
        `${engine.name} would have no enabled connection left, so it could not translate until you add one.`,
      );
    }
  }
  return effects;
}

/** What the Translation page would lose, asked only while the dialog is open and only of a server that has it. */
function useEngineEffects(pluginId: string | null, keepData: boolean) {
  const query = useQuery({
    queryKey: [QueryKeys.Translation, "plugin-hub-remove", pluginId],
    queryFn: async () => {
      try {
        return await withInlineErrors(() => api.translation.state());
      } catch {
        return null;
      }
    },
    enabled: pluginId !== null,
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });
  return pluginId ? engineEffects(query.data, pluginId, keepData) : [];
}

interface RemoveDialogProps {
  /** The plugin to remove, or null while nothing is. */
  row: PluginRow | null;
  busy: boolean;
  problem: string;
  /** What the person chose to do with the plugin's saved settings and data folder. */
  onConfirm: (keepData: boolean) => void;
  onClose: () => void;
  onClosed?: () => void;
}

/**
 * The confirmation for a removal. It lists the plugins that need this one first and refuses to go on while
 * any does, says what the Translation page loses,. Focus starts on the heading, so nothing is one keypress from removing.
 */
export const RemoveDialog: FunctionComponent<RemoveDialogProps> = ({
  row,
  busy,
  problem,
  onConfirm,
  onClose,
  onClosed,
}) => {
  const shown = useRef(row);
  if (row) {
    shown.current = row;
  }
  const current = row ?? shown.current;
  // Keeping is the default, and every removal starts from it.
  const [choice, setChoice] = useState<"keep" | "delete">("keep");
  const effects = useEngineEffects(row?.id ?? null, choice === "keep");
  const openedFor = row?.id ?? null;
  useEffect(() => {
    if (openedFor !== null) {
      setChoice("keep");
    }
  }, [openedFor]);
  const blockers = current?.dependents ?? [];
  const blocked = blockers.length > 0;
  return (
    <Modal
      opened={row !== null}
      onClose={busy ? () => undefined : onClose}
      withCloseButton={!busy}
      closeButtonProps={{ "aria-label": "Close" }}
      closeOnEscape={!busy}
      closeOnClickOutside={!busy}
      onExitTransitionEnd={onClosed}
      centered
      title={
        <span className={styles.dialogHeading} tabIndex={-1} data-autofocus>
          {current ? `Remove ${current.name}?` : ""}
        </span>
      }
    >
      {current && (
        <Stack gap="md">
          {blocked ? (
            <Alert color="red" role="alert" title="Another plugin needs it">
              {blockers.join(", ")} {blockers.length === 1 ? "needs" : "need"}{" "}
              {current.name}. Remove {blockers.length === 1 ? "that" : "those"}{" "}
              first.
            </Alert>
          ) : (
            <Radio.Group
              value={choice}
              onChange={(value) =>
                setChoice(value === "delete" ? "delete" : "keep")
              }
              label="What happens to its data"
              description="The settings you saved for it and its data folder."
            >
              <Stack gap="xs" mt="xs">
                <Radio
                  value="keep"
                  label="Keep its settings and data"
                  description="Installing it again picks up where you left off."
                />
                <Radio
                  value="delete"
                  label="Delete its settings and data"
                  description="Installing it again starts clean. This cannot be undone."
                />
              </Stack>
            </Radio.Group>
          )}
          {!blocked && effects.length > 0 && (
            <Alert color="yellow" variant="light" role="note">
              <ul
                className={styles.effects}
                aria-label="What the Translation page loses"
              >
                {effects.map((effect) => (
                  <li key={effect}>{effect}</li>
                ))}
              </ul>
            </Alert>
          )}
          {problem && (
            <Alert color="red" role="alert">
              {problem}
            </Alert>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              color="red"
              onClick={() => onConfirm(choice === "keep")}
              loading={busy}
              disabled={blocked}
            >
              Remove
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
};
