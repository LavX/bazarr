import { FunctionComponent, useEffect, useState } from "react";
import {
  Button,
  Card,
  Divider,
  Group,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useDistSaveTiers, useDistTiers } from "@/apis/hooks";
import type { DistTier } from "@/apis/raw/distributionHub";
import { QueryOverlay } from "@/components/async";
import { WINDOW_LABELS, WINDOWS } from "./limits";

const TiersPanel: FunctionComponent = () => {
  const tiers = useDistTiers();
  const saveTiers = useDistSaveTiers();

  const [draft, setDraft] = useState<Record<string, DistTier>>({});
  const [defaultTier, setDefaultTier] = useState<string>("free");

  useEffect(() => {
    if (tiers.data) {
      setDraft(structuredClone(tiers.data.tiers));
      setDefaultTier(tiers.data.default_tier);
    }
  }, [tiers.data]);

  const setLimit = (
    tierId: string,
    kind: "search" | "download",
    window: keyof DistTier["search"],
    value: number,
  ) => {
    setDraft((d) => {
      const tier = d[tierId];
      if (!tier) {
        return d;
      }
      return {
        ...d,
        [tierId]: {
          ...tier,
          [kind]: { ...tier[kind], [window]: value },
        },
      };
    });
  };

  const tierIds = Object.keys(draft);

  return (
    <QueryOverlay result={tiers}>
      <Stack>
        <Group justify="space-between" align="flex-end">
          <Select
            label="Default tier for new keys"
            data={tierIds.map((id) => ({
              value: id,
              label: draft[id]?.label ?? id,
            }))}
            value={defaultTier}
            onChange={(v) => v && setDefaultTier(v)}
            w={240}
          />
          <Button
            loading={saveTiers.isPending}
            onClick={() =>
              saveTiers.mutate({ default_tier: defaultTier, tiers: draft })
            }
          >
            Save tiers
          </Button>
        </Group>

        <Text size="xs" c="dimmed">
          A limit of 0 means unlimited for that window. The legacy Default key
          is on the Unlimited tier so existing integrations are never throttled.
        </Text>

        {tierIds.map((id) => {
          const tier = draft[id];
          return (
            <Card key={id} withBorder padding="md" radius="md">
              <Title order={5}>{tier.label ?? id}</Title>
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
                        value={tier[kind][w]}
                        onChange={(v) =>
                          setLimit(
                            id,
                            kind,
                            w,
                            v === "" || v == null ? 0 : Number(v),
                          )
                        }
                      />
                    ))}
                  </SimpleGrid>
                </div>
              ))}
            </Card>
          );
        })}
      </Stack>
    </QueryOverlay>
  );
};

export default TiersPanel;
