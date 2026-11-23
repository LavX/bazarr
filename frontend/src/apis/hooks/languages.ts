import { useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

export function useLanguages(history?: boolean, enabled = true) {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Languages, history ?? false],
    queryFn: () => api.system.languages(history),
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
