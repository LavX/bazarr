import { FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  ComboboxItem,
  em,
  Flex,
  Image,
  OptionsFilter,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useServerSearch } from "@/apis/hooks";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { InstanceBadge } from "@/components/bazarr";
import { useDebouncedValue } from "@/utilities";

type SearchResultItem = {
  value: string;
  label: string;
  link: string;
  poster: string | null;
  type: string;
  instanceId?: number;
};

function useSearch(query: string) {
  const debouncedQuery = useDebouncedValue(query, 500);
  const { data } = useServerSearch(debouncedQuery, debouncedQuery.length > 0);

  return useMemo<SearchResultItem[]>(
    () =>
      data?.map((v) => {
        const { link, label, poster, type, value } = (() => {
          if (v.sonarrSeriesId) {
            // Route by the canonical local id (#156); fall back to the upstream
            // id for safety. Equal on a single default instance.
            const seriesId = v.id ?? v.sonarrSeriesId;
            return {
              poster: v.poster,
              link: `/series/${seriesId}`,
              type: "show",
              label: `${v.title} (${v.year})`,
              value: `s-${seriesId}`,
            };
          }

          if (v.radarrId) {
            const movieId = v.id ?? v.radarrId;
            return {
              poster: v.poster,
              link: `/movies/${movieId}`,
              type: "movie",
              value: `m-${movieId}`,
              label: `${v.title} (${v.year})`,
            };
          }

          throw new Error("Unknown search result");
        })();

        return {
          value: value,
          poster: poster,
          label: label,
          type: type,
          link: link,
          instanceId: v.arr_instance_id,
        };
      }) ?? [],
    [data],
  );
}

const optionsFilter: OptionsFilter = ({ options, search }) => {
  const lowercaseSearch = search.toLowerCase();
  const normalizedSearch = search
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();

  return (options as ComboboxItem[]).filter((option) => {
    return (
      option.label.toLowerCase().includes(lowercaseSearch) ||
      option.label
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .includes(normalizedSearch)
    );
  });
};

const Search: FunctionComponent = () => {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");

  const results = useSearch(query);
  const sonarrInstances = useArrInstanceLabels("sonarr");
  const radarrInstances = useArrInstanceLabels("radarr");

  const isMobile = useMediaQuery(`(max-width: ${em(750)})`);

  return (
    <Select
      placeholder="Search"
      withCheckIcon={false}
      leftSection={<FontAwesomeIcon icon={faSearch} />}
      rightSection={<></>}
      size="sm"
      searchable
      scrollAreaProps={{ type: "auto" }}
      maxDropdownHeight={400}
      data={results}
      value={query}
      onSearchChange={(a) => {
        setQuery(a);
      }}
      onBlur={() => setQuery("")}
      filter={optionsFilter}
      onOptionSubmit={(option) => {
        navigate(results.find((a) => a.value === option)?.link || "/");
      }}
      renderOption={(input) => {
        const result = results.find((r) => r.value === input.option.value);
        const instanceLabels =
          result?.type === "show" ? sonarrInstances : radarrInstances;

        return (
          <Flex>
            <Image src={result?.poster} w={55} h={70} />
            <Stack gap={4} px="xs" justify="center" miw={0}>
              <Text size={isMobile ? "xs" : "md"} lineClamp={3}>
                {result?.label}
              </Text>
              {instanceLabels.multiInstance && (
                <InstanceBadge
                  instanceId={result?.instanceId}
                  defaultId={instanceLabels.defaultId}
                  nameById={instanceLabels.nameById}
                />
              )}
            </Stack>
          </Flex>
        );
      }}
    />
  );
};

export default Search;
