const credentialKey = "settings-discover-tmdb_access_token";

export function settingsLogValue(key: string, value: unknown) {
  return key === credentialKey ? "[redacted]" : value;
}

export function settingsLogValues(values: LooseObject) {
  return Object.fromEntries(
    Object.entries(values).map(([key, value]) => [
      key,
      settingsLogValue(key, value),
    ]),
  );
}
