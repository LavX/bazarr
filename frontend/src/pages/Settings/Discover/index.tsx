import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  Group,
  NativeSelect,
  PasswordInput,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import api from "@/apis/raw";
import { useIsLoading } from "@/contexts";
import Layout from "@/pages/Settings/components/Layout";
import { Section } from "@/pages/Settings/components/Section";
import {
  useFormActions,
  useFormValues,
} from "@/pages/Settings/utilities/FormValues";
import { useSettings } from "@/pages/Settings/utilities/SettingsProvider";
import type { MetadataResponse } from "@/types/discover";

const tokenKey = "settings-discover-tmdb_access_token";
const localeKey = "settings-discover-locale";

function DiscoverIntegration() {
  const settings = useSettings();
  const saving = useIsLoading();
  const form = useFormValues();
  const { setValue } = useFormActions();
  const configured = settings?.discover?.tmdb_configured ?? false;
  const revision = settings?.discover?.metadata_revision;
  const draft = form.values.settings[tokenKey] as string | undefined;
  const locale = (form.values.settings[localeKey] ??
    settings?.discover?.locale ??
    "en-US") as string;
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
    } catch {
      if (!attempt.signal.aborted) setFailed(true);
    } finally {
      if (!attempt.signal.aborted) setChecking(false);
    }
  };

  return (
    <Stack gap="md" maw={720}>
      <Title order={2}>Discover</Title>
      <Text>
        Browse movie titles from TMDB, including films outside your library.
        Library connections and subtitle providers are set up separately.
      </Text>
      <Section header="Movie metadata">
        <Text fw={600}>
          {configured
            ? "Saved TMDB token configured"
            : "TMDB is not configured"}
        </Text>
        <PasswordInput
          disabled={saving}
          label="TMDB API Read Access Token"
          autoComplete="new-password"
          value={draft ?? ""}
          maxLength={4096}
          description="Paste a replacement token here. The saved token stays private on the server."
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
              ? "Check saved connection"
              : "Check draft connection"}
          </Button>
          {configured && (
            <Button
              disabled={saving}
              type="button"
              variant="subtle"
              color="red"
              mih={44}
              onClick={() => setValue("", tokenKey)}
            >
              Remove saved token
            </Button>
          )}
          {draft !== undefined && (
            <Button
              disabled={saving}
              type="button"
              variant="subtle"
              mih={44}
              onClick={clearDraft}
            >
              Cancel token change
            </Button>
          )}
        </Group>
        {draft !== undefined && (
          <Text size="sm">
            {draft
              ? "Replacement pending save."
              : "Removal pending save. Global movie browsing will be unavailable."}
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
          Get a TMDB API Read Access Token
        </Anchor>
        <NativeSelect
          disabled={saving}
          label="Metadata language"
          value={locale}
          styles={{ input: { minHeight: 44 } }}
          data={[
            ...new Set([
              "en-US",
              "en-GB",
              "hu-HU",
              "de-DE",
              "fr-FR",
              "es-ES",
              locale,
            ]),
          ].map((value) => ({ value, label: value }))}
          description="Used for titles and overviews. Subtitle language is chosen separately in Discover."
          onChange={(event) => setValue(event.currentTarget.value, localeKey)}
        />
      </Section>
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

export default function DiscoverSettings() {
  return (
    <Layout name="Discover">
      <DiscoverIntegration />
    </Layout>
  );
}
