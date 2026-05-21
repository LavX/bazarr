import {
  ALL_LANGUAGE_OPTIONS as ALL_LANG_OPTIONS,
  getLanguageName,
  getProviderLanguages,
} from "./provider-languages";

export type AuthKind = "none" | "account" | "apikey" | "cookies";

export const AUTH_LABEL: Record<AuthKind, string> = {
  none: "No signup",
  account: "Account",
  apikey: "API key",
  cookies: "Cookies",
};

export const AUTH_FILTERS: { value: AuthKind | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "none", label: "No signup" },
  { value: "account", label: "Account" },
  { value: "apikey", label: "API key" },
  { value: "cookies", label: "Cookies" },
];

interface InputLike {
  key?: string;
  type?: string;
}

const classifyKeys = (keys: string[]): AuthKind => {
  if (keys.length === 0) return "none";
  if (keys.some((k) => k === "username" || k === "password" || k === "email"))
    return "account";
  if (
    keys.some(
      (k) =>
        k === "passkey" || k === "token" || k === "api_key" || k === "apikey",
    )
  )
    return "apikey";
  if (keys.some((k) => k === "cookies")) return "cookies";
  return "none";
};

export const detectAuthFromInputs = (
  inputs: readonly InputLike[] | undefined,
): AuthKind => {
  if (!inputs) return "none";
  const credKeys = inputs
    .filter((i) => i.type !== "testbutton" && i.type !== "switch")
    .map((i) => (i.key ?? "").toLowerCase());
  return classifyKeys(credKeys);
};

export const hasTestButton = (
  inputs: readonly InputLike[] | undefined,
): boolean => (inputs ?? []).some((i) => i.type === "testbutton");

export const detectAuthFromManifest = (
  manifest: LooseObject | null | undefined,
): AuthKind => {
  if (!manifest) return "none";
  const schema = manifest.config_schema as LooseObject | undefined;
  if (!schema) return "none";
  const props =
    (schema.properties as Record<string, LooseObject> | undefined) ?? {};
  const keys: string[] = [];
  for (const [k, v] of Object.entries(props)) {
    const lower = k.toLowerCase();
    if (typeof v === "object" && v && (v as LooseObject).type === "boolean") {
      continue;
    }
    keys.push(lower);
  }
  return classifyKeys(keys);
};

export const getLanguagesFromManifest = (
  manifest: LooseObject | null | undefined,
): string[] => {
  if (!manifest) return [];
  const langs = manifest.languages;
  if (!Array.isArray(langs)) return [];
  return langs.filter((l): l is string => typeof l === "string");
};

export interface ProviderMeta {
  auth: AuthKind;
  testable: boolean;
  languages: string[];
}

export const getShippedMeta = (
  providerKey: string,
  inputs: readonly InputLike[] | undefined,
): ProviderMeta => ({
  auth: detectAuthFromInputs(inputs),
  testable: hasTestButton(inputs),
  languages: getProviderLanguages(providerKey),
});

export { ALL_LANG_OPTIONS as ALL_LANGUAGE_OPTIONS, getLanguageName };
