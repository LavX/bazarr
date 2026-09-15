import { FunctionComponent } from "react";
import { SimpleGrid } from "@mantine/core";
import { useLanguages, useSystemProviders } from "@/apis/hooks";
import { Selector } from "@/components";
import { StatisticsFilters } from "@/pages/Statistics/filters";
import { actionOptions, timeFrameOptions } from "@/pages/Statistics/options";
import { useSelectorOptions } from "@/utilities";

interface Props {
  value: StatisticsFilters;
  onChange: (next: StatisticsFilters) => void;
}

const MetricsFilters: FunctionComponent<Props> = ({ value, onChange }) => {
  // history=true lists the providers that actually appear in download history,
  // which is the useful set to filter by. The throttle-state variant would
  // offer providers that have never delivered anything.
  const { data: providers } = useSystemProviders(true);
  const providerOptions = useSelectorOptions(providers ?? [], (v) => v.name);

  const { data: languages } = useLanguages(true);
  const languageOptions = useSelectorOptions(languages ?? [], (v) => v.name);

  return (
    <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
      <Selector
        placeholder="Time..."
        options={timeFrameOptions}
        value={value.timeFrame}
        onChange={(v) => onChange({ ...value, timeFrame: v ?? "month" })}
      ></Selector>
      <Selector
        placeholder="Action..."
        clearable
        options={actionOptions}
        value={value.action}
        onChange={(action) => onChange({ ...value, action })}
      ></Selector>
      <Selector
        {...providerOptions}
        placeholder="Provider..."
        clearable
        value={value.provider}
        onChange={(provider) => onChange({ ...value, provider })}
      ></Selector>
      <Selector
        {...languageOptions}
        placeholder="Language..."
        clearable
        value={value.language}
        onChange={(language) => onChange({ ...value, language })}
      ></Selector>
    </SimpleGrid>
  );
};

export default MetricsFilters;
