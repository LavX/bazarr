import { FunctionComponent } from "react";
import { Container, Stack } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faTrash } from "@fortawesome/free-solid-svg-icons";
import { useEpisodeBlacklist, useEpisodeDeleteBlacklist } from "@/apis/hooks";
import { useSourceFilter } from "@/apis/hooks/mediaSources";
import { useAppTitle } from "@/apis/hooks/site";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import { blacklistSourceLabels } from "@/pages/Blacklist/row";
import useSourceOptions from "@/pages/Blacklist/useSourceOptions";
import SourceFilter from "@/pages/views/SourceFilter";
import Table from "./table";

const BlacklistSeriesView: FunctionComponent = () => {
  const filter = useSourceFilter("series", "sonarr", {
    includeDisabledSources: true,
  });
  const { selected, includeSources, labels, onChange } = filter;
  const sourceOptions = useSourceOptions("episode", includeSources);
  const scope: Blacklist.Scope = {
    includeSources,
    sourceKeys: selected ? [selected] : undefined,
  };
  const blacklist = useEpisodeBlacklist(scope);
  const { mutateAsync } = useEpisodeDeleteBlacklist();

  useDocumentTitle(`Series Excluded - ${useAppTitle()}`);

  const { data } = blacklist;
  return (
    <QueryOverlay result={blacklist}>
      <Container fluid px={0}>
        <Stack>
          <Toolbox>
            <Toolbox.MutateButton
              icon={faTrash}
              disabled={!data?.length || blacklist.isFetching}
              promise={() => mutateAsync({ all: true, ...scope })}
            >
              Remove All
            </Toolbox.MutateButton>
            <SourceFilter
              labels={blacklistSourceLabels(
                labels,
                includeSources ? (sourceOptions.data ?? []) : [],
              )}
              value={selected}
              onChange={onChange}
              includeSources={includeSources}
              showSingleSource
            />
          </Toolbox>
          <Table blacklist={data ?? []} filter={filter} />
        </Stack>
      </Container>
    </QueryOverlay>
  );
};

export default BlacklistSeriesView;
