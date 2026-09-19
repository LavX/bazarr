import {
  createContext,
  PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

export interface SearchMatch {
  id: string;
  title: string;
  detail?: string;
  select: () => void;
}

export interface SearchSource {
  id: string;
  label: string;
  search: (query: string) => SearchMatch[];
}

const SearchSources = createContext<{
  sources: SearchSource[];
  register: (source: SearchSource) => () => void;
}>({ sources: [], register: () => () => undefined });

export function UniversalSearchProvider({ children }: PropsWithChildren) {
  const [sources, setSources] = useState<SearchSource[]>([]);
  const register = useCallback((source: SearchSource) => {
    setSources((current) => [
      ...current.filter((item) => item.id !== source.id),
      source,
    ]);
    return () =>
      setSources((current) => current.filter((item) => item !== source));
  }, []);
  return (
    <SearchSources.Provider value={{ sources, register }}>
      {children}
    </SearchSources.Provider>
  );
}

export function useSearchSource(source: SearchSource) {
  const { register } = useContext(SearchSources);
  useEffect(() => register(source), [register, source]);
}

export function useSearchSources() {
  return useContext(SearchSources).sources;
}
