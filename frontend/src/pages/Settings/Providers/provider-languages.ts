import data from "./provider-languages.json";

export interface ProviderLanguagesData {
  languages: Record<string, string>;
  providers: Record<string, string[]>;
}

const typed = data as ProviderLanguagesData;

export const PROVIDER_LANGUAGES = typed.providers;
export const LANGUAGE_NAMES = typed.languages;

export interface LanguageOption {
  code: string;
  name: string;
}

export const ALL_LANGUAGE_OPTIONS: LanguageOption[] = Object.entries(
  LANGUAGE_NAMES,
)
  .map(([code, name]) => ({ code, name }))
  .sort((a, b) => a.name.localeCompare(b.name));

export function getProviderLanguages(providerKey: string): string[] {
  return PROVIDER_LANGUAGES[providerKey] ?? [];
}

export function getLanguageName(code: string): string {
  return LANGUAGE_NAMES[code] ?? code;
}
