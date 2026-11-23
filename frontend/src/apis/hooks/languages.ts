import { useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

export function useLanguages(
  history = false,
  includeSources = false,
  enabled = true,
) {
  return useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Languages,
      history,
      ...(includeSources ? [true] : []),
    ],
    queryFn: () => api.system.languages(history, includeSources),
    staleTime: Infinity,
    enabled,
  });
}

export function useAudioLanguages(enabled = true, includeSources = false) {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Languages, "audio", includeSources],
    queryFn: () => api.system.audioLanguages(includeSources),
    placeholderData: undefined,
    staleTime: 60_000,
    enabled,
  });
}

export function useLanguageProfiles() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.LanguagesProfiles],
    queryFn: () => api.system.languagesProfileList(),
    staleTime: Infinity,
  });
}
