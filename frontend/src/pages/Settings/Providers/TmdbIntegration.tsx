import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  Group,
  PasswordInput,
  Stack,
  Text,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { useIsLoading } from "@/contexts";
import {
  useFormActions,
  useFormValues,
} from "@/pages/Settings/utilities/FormValues";
import { useSettings } from "@/pages/Settings/utilities/SettingsProvider";
import type { MetadataResponse } from "@/types/discover";

const tokenKey = "settings-discover-tmdb_access_token";

export default function TmdbIntegration() {
  const settings = useSettings();
  const saving = useIsLoading();
  const form = useFormValues();
  const { setValue } = useFormActions();
  // The built-in key makes metadata available on every install, so only an
  // explicit false from the server says otherwise.
  const configured = settings?.discover?.tmdb_configured !== false;
  // Metadata being available is the built-in key's doing, so it is not evidence
  // that the reader has anything saved. Only a saved key can be removed.
  const tokenStored = settings?.discover?.tmdb_token_stored ?? false;
  const revision = settings?.discover?.metadata_revision;
  // A saved key TMDB rejects never stops Discover: the server moves browsing
  // to the built-in key. This field is the one place that says so. The key is
  // outside the metadata namespace, which a save retires wholesale.
  const keyStatus = useQuery({
    queryKey: [QueryKeys.Discover, "tmdb-key-status", revision],
    queryFn: ({ signal }) => api.discover.metadata("status", signal),
    enabled: tokenStored,
    staleTime: 0,
    retry: false,
  });
  const rejected = tokenStored && keyStatus.data?.override_rejected === true;
  const draft = form.values.settings[tokenKey] as string | undefined;
  const [check, setCheck] = useState<MetadataResponse | null>(null);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    request.current?.abort();
    setCheck(null);
    setFailed(false);
    setChecking(false);
    return () => request.current?.abort();
  }, [draft, revision]);

  const clearDraft = () => {
    form.setValues((current) => {
      const next = { ...current.settings };
      delete next[tokenKey];
      return { ...current, settings: next };
    });
  };
  const test = async () => {
    request.current?.abort();
    const attempt = new AbortController();
    request.current = attempt;
    setChecking(true);
    setCheck(null);
    setFailed(false);
    try {
      const response = await api.discover.testMetadata(draft, attempt.signal);
      if (!attempt.signal.aborted) setCheck(response);
      // Checking the saved key is also what clears or records its rejection.
      if (draft === undefined && tokenStored) void keyStatus.refetch();
    } catch {
      if (!attempt.signal.aborted) setFailed(true);
    } finally {
      if (!attempt.signal.aborted) setChecking(false);
    }
  };

  return (
    <Stack gap="md" maw={720}>
      <Text fw={600}>
        {configured
          ? "TMDB metadata is available"
          : "TMDB metadata is unavailable in this build"}
      </Text>
      <PasswordInput
        disabled={saving}
        label="TMDB API key"
        autoComplete="new-password"
        value={draft ?? ""}
        maxLength={4096}
        description="Optional. Uses the built-in key unless you save your own TMDB v3 API key."
        styles={{
          input: { minHeight: 44 },
          innerInput: { minHeight: 44 },
          visibilityToggle: { minWidth: 44, minHeight: 44 },
        }}
        onChange={(event) => {
          const value = event.currentTarget.value;
          if (value) setValue(value, tokenKey);
          else clearDraft();
        }}
      />
      {rejected && draft === undefined && (
        <Alert color="yellow">
          TMDB rejected your saved key, so Discover is using the built-in key.
          Replace the key or remove it.
        </Alert>
      )}
      <Group>
        <Button
          disabled={saving}
          type="button"
          variant="light"
          miw={44}
          mih={44}
          loading={checking}
          onClick={() => void test()}
        >
          {draft === undefined
            ? tokenStored
              ? "Check saved connection"
              : "Check built-in connection"
            : "Check draft connection"}
        </Button>
        {tokenStored && (
          <Button
            disabled={saving}
            type="button"
            variant="default"
            color="red"
            mih={44}
            onClick={() => setValue("", tokenKey)}
          >
            Remove saved key
          </Button>
        )}
        {draft !== undefined && (
          <Button
            disabled={saving}
            type="button"
            variant="default"
            mih={44}
            onClick={clearDraft}
          >
            Cancel key change
          </Button>
        )}
      </Group>
      {draft !== undefined && (
        <Text size="sm">
          {draft
            ? "Replacement pending save."
            : "Removal pending save. Browsing continues on the built-in key."}
        </Text>
      )}
      <div role="status" aria-live="polite">
        {checking && <Text>Checking TMDB connection.</Text>}
        {check && (
          <Alert color={check.status === "available" ? "green" : "yellow"}>
            {check.message}
            {check.checked_at && (
              <Text size="sm">
                Checked{" "}
                <time dateTime={check.checked_at}>
                  {new Date(check.checked_at).toLocaleString()}
                </time>
              </Text>
            )}
            {draft !== undefined && (
              <Text size="sm">This check does not save your changes.</Text>
            )}
          </Alert>
        )}
        {failed && (
          <Alert color="yellow">
            Connection check failed. Retry the check.
          </Alert>
        )}
      </div>
      <Anchor
        c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
        href="https://www.themoviedb.org/settings/api"
        target="_blank"
        rel="noreferrer"
        py="sm"
      >
        Get your own TMDB API key
      </Anchor>
      <Text size="sm">
        Movie metadata from{" "}
        <Anchor
          c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
          href="https://www.themoviedb.org"
          target="_blank"
          rel="noreferrer"
        >
          TMDB
        </Anchor>
        . This product uses the TMDB API but is not endorsed or certified by
        TMDB.
      </Text>
    </Stack>
  );
}
