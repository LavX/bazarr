import { FunctionComponent } from "react";
import { Container, Stack } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faTrash } from "@fortawesome/free-solid-svg-icons";
import { useSourceFilter } from "@/apis/hooks/mediaSources";
import {
  useMovieBlacklist,
  useMovieDeleteBlacklist,
} from "@/apis/hooks/movies";
import { useAppTitle } from "@/apis/hooks/site";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import { blacklistSourceLabels } from "@/pages/Blacklist/row";
import useSourceOptions from "@/pages/Blacklist/useSourceOptions";
import SourceFilter from "@/pages/views/SourceFilter";
import Table from "./table";

const BlacklistMoviesView: FunctionComponent = () => {
  const filter = useSourceFilter("movie", "radarr", {
    includeDisabledSources: true,
    includeRetainedSources: true,
  });
  const { selected, includeSources, labels, onChange } = filter;
  const sourceOptions = useSourceOptions("movie", includeSources);
  const scope: Blacklist.Scope = {
    includeSources,
    sourceKeys: selected ? [selected] : undefined,
  };
  const blacklist = useMovieBlacklist(scope);
  const { data } = blacklist;

  const { mutateAsync } = useMovieDeleteBlacklist();

  useDocumentTitle(`Movies Excluded - ${useAppTitle()}`);

  return (
    <Container fluid px={0}>
      <QueryOverlay result={blacklist}>
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
      </QueryOverlay>
    </Container>
  );
};

export default BlacklistMoviesView;
