import { FunctionComponent, useEffect, useState } from "react";
import {
  Button,
  Divider,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import type {
  DistKey,
  DistKindLimits,
  DistTiersResponse,
} from "@/apis/raw/distributionHub";
import { emptyKindLimits, WINDOW_LABELS, WINDOWS } from "./limits";

export interface KeyDraft {
  name: string;
  tier: string;
  timeout_seconds: number | null;
  note: string | null;
  excluded_providers: string[];
  allowed_providers: string[];
  custom_enabled: boolean;
  custom_limits: DistKindLimits;
}

interface Props {
  opened: boolean;
  onClose: () => void;
  // null => create mode
  editing: DistKey | null;
  tiers: DistTiersResponse | undefined;
  providers: string[];
  saving: boolean;
  onSubmit: (draft: KeyDraft) => void;
}

function draftFromKey(key: DistKey | null, defaultTier: string): KeyDraft {
  if (!key) {
    return {
      name: "",
      tier: defaultTier,
      timeout_seconds: null,
      note: "",
      excluded_providers: [],
      allowed_providers: [],
      custom_enabled: false,
      custom_limits: emptyKindLimits(),
    };
  }
  const base = emptyKindLimits();
  const cl = key.custom_limits;
  if (cl?.search) base.search = { ...base.search, ...cl.search };
  if (cl?.download) base.download = { ...base.download, ...cl.download };
  return {
    name: key.name,
    tier: key.tier,
    timeout_seconds: key.timeout_seconds,
    note: key.note ?? "",
    excluded_providers: key.excluded_providers ?? [],
    allowed_providers: key.allowed_providers ?? [],
    custom_enabled: cl != null,
    custom_limits: base,
  };
}

const KeyEditorModal: FunctionComponent<Props> = ({
  opened,
  onClose,
  editing,
  tiers,
  providers,
  saving,
  onSubmit,
}) => {
  const defaultTier = tiers?.default_tier ?? "free";
  const [draft, setDraft] = useState<KeyDraft>(
    draftFromKey(editing, defaultTier),
  );

  useEffect(() => {
    if (opened) {
      setDraft(draftFromKey(editing, defaultTier));
    }
  }, [opened, editing, defaultTier]);

  const tierOptions = Object.entries(tiers?.tiers ?? {}).map(([id, t]) => ({
    value: id,
    label: t.label ?? id,
  }));

  const setLimit = (
    kind: "search" | "download",
    window: keyof DistKindLimits["search"],
    value: number,
  ) => {
    setDraft((d) => ({
      ...d,
      custom_limits: {
        ...d.custom_limits,
        [kind]: { ...d.custom_limits[kind], [window]: value },
      },
    }));
  };

  const canSubmit = draft.name.trim().length > 0;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={editing ? `Edit ${editing.name}` : "Create API key"}
      size="lg"
    >
      <Stack gap="sm">
        <TextInput
          label="Name"
          placeholder="e.g. my-subtitle-site"
          required
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })}
        />
        <Select
          label="Tier"
          data={tierOptions}
          value={draft.tier}
          onChange={(v) => v && setDraft({ ...draft, tier: v })}
        />
        <MultiSelect
          label="Excluded providers (default for this key)"
          description="Searches with this key skip these providers unless the request overrides it."
          data={providers}
          searchable
          clearable
          value={draft.excluded_providers}
          onChange={(v) => setDraft({ ...draft, excluded_providers: v })}
        />
        <MultiSelect
          label="Allowed providers (default for this key)"
          description="Restrict this key to ONLY these providers. Leave empty to allow all. Exclusions above still apply on top. A per-request only_providers value overrides this."
          data={providers}
          searchable
          clearable
          value={draft.allowed_providers}
          onChange={(v) => setDraft({ ...draft, allowed_providers: v })}
        />
        <NumberInput
          label="Search timeout override (seconds)"
          description="Leave empty to use the global timeout. 5-120."
          min={5}
          max={120}
          value={draft.timeout_seconds ?? ""}
          onChange={(v) =>
            setDraft({
              ...draft,
              timeout_seconds: v === "" || v == null ? null : Number(v),
            })
          }
        />

        <Switch
          label="Custom rate limits (override the tier)"
          checked={draft.custom_enabled}
          onChange={(e) =>
            setDraft({ ...draft, custom_enabled: e.currentTarget.checked })
          }
        />
        {draft.custom_enabled && (
          <Stack gap="xs">
            <Text size="xs" c="dimmed">
              0 means unlimited for that window.
            </Text>
            {(["search", "download"] as const).map((kind) => (
              <div key={kind}>
                <Divider
                  my="xs"
                  label={kind === "search" ? "Search" : "Download"}
                  labelPosition="left"
                />
                <SimpleGrid cols={{ base: 2, sm: 4 }}>
                  {WINDOWS.map((w) => (
                    <NumberInput
                      key={w}
                      label={WINDOW_LABELS[w]}
                      min={0}
                      value={draft.custom_limits[kind][w]}
                      onChange={(v) =>
                        setLimit(kind, w, v === "" || v == null ? 0 : Number(v))
                      }
                    />
                  ))}
                </SimpleGrid>
              </div>
            ))}
          </Stack>
        )}

        <Textarea
          label="Note"
          autosize
          minRows={1}
          value={draft.note ?? ""}
          onChange={(e) => setDraft({ ...draft, note: e.currentTarget.value })}
        />

        <Group justify="flex-end" mt="sm">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={saving}
            disabled={!canSubmit}
            onClick={() => onSubmit(draft)}
          >
            {editing ? "Save" : "Create key"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
};

export default KeyEditorModal;
