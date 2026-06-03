import { FunctionComponent, useEffect, useState } from "react";
import {
  Button,
  Card,
  Group,
  NumberInput,
  Stack,
  Switch,
  Text,
  Title,
} from "@mantine/core";
import {
  useDistRegenerate,
  useDistSaveSettings,
  useDistSettings,
} from "@/apis/hooks";
import type { DistSettings } from "@/apis/raw/distributionHub";
import { QueryOverlay } from "@/components/async";

const SettingsPanel: FunctionComponent = () => {
  const settings = useDistSettings();
  const saveSettings = useDistSaveSettings();
  const regenerate = useDistRegenerate();

  const [draft, setDraft] = useState<DistSettings | null>(null);

  useEffect(() => {
    if (settings.data) {
      setDraft(settings.data);
    }
  }, [settings.data]);

  return (
    <QueryOverlay result={settings}>
      <Stack>
        {draft && (
          <>
            <Card withBorder padding="md" radius="md">
              <Title order={5} mb="sm">
                Endpoint
              </Title>
              <Stack gap="sm">
                <Switch
                  label="I understand this endpoint must not be exposed to the public internet and I am responsible for provider ToS compliance."
                  checked={draft.consent}
                  onChange={(e) =>
                    setDraft({ ...draft, consent: e.currentTarget.checked })
                  }
                />
                <Switch
                  label="Enable the Distribution Hub endpoint"
                  checked={draft.enabled}
                  onChange={(e) =>
                    setDraft({ ...draft, enabled: e.currentTarget.checked })
                  }
                />
                <Switch
                  label="Rate-limit search requests"
                  description="When off, search is metered but never throttled."
                  checked={draft.search_rate_limit_enabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      search_rate_limit_enabled: e.currentTarget.checked,
                    })
                  }
                />
                <Switch
                  label="Serve subtitles already on disk"
                  checked={draft.serve_local_subs}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      serve_local_subs: e.currentTarget.checked,
                    })
                  }
                />
                <Group grow>
                  <NumberInput
                    label="Global search timeout (seconds)"
                    min={5}
                    max={120}
                    value={draft.search_timeout_seconds}
                    onChange={(v) =>
                      setDraft({
                        ...draft,
                        search_timeout_seconds: Number(v) || 5,
                      })
                    }
                  />
                  <NumberInput
                    label="Usage retention (days)"
                    description="Older usage buckets are pruned."
                    min={35}
                    max={1000}
                    value={draft.usage_retention_days}
                    onChange={(v) =>
                      setDraft({
                        ...draft,
                        usage_retention_days: Number(v) || 400,
                      })
                    }
                  />
                </Group>
                <Group justify="flex-end">
                  <Button
                    loading={saveSettings.isPending}
                    onClick={() =>
                      saveSettings.mutate({
                        enabled: draft.enabled,
                        consent: draft.consent,
                        search_rate_limit_enabled:
                          draft.search_rate_limit_enabled,
                        serve_local_subs: draft.serve_local_subs,
                        search_timeout_seconds: draft.search_timeout_seconds,
                        usage_retention_days: draft.usage_retention_days,
                      })
                    }
                  >
                    Save settings
                  </Button>
                </Group>
              </Stack>
            </Card>

            <Card withBorder padding="md" radius="md">
              <Title order={5} mb="sm">
                Secrets
              </Title>
              <Group justify="space-between" align="center">
                <Text size="sm" c="dimmed">
                  Regenerating rotates the signing secrets and the shared legacy
                  token. The Default key is automatically re-pointed at the new
                  token; named keys are unaffected.
                </Text>
                <Button
                  color="orange"
                  variant="light"
                  loading={regenerate.isPending}
                  onClick={() => regenerate.mutate()}
                >
                  Regenerate secrets
                </Button>
              </Group>
              <Text size="xs" c="dimmed" mt="sm">
                This endpoint implements a REST API shape used by
                OpenSubtitles.com for interoperability with existing
                OpenSubtitles-compatible media-center plugins. Bazarr+ is not
                affiliated with or endorsed by OpenSubtitles.com.
              </Text>
            </Card>
          </>
        )}
      </Stack>
    </QueryOverlay>
  );
};

export default SettingsPanel;
