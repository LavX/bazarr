/* eslint-disable camelcase */

import { useCallback, useState } from "react";
import { AxiosError } from "axios";
import { useSettingsMutation } from "@/apis/hooks";
import {
  SaveMediaServerInput,
  useSaveMediaServerInstance,
} from "@/apis/hooks/mediaServers";
import type { MediaServerKind } from "@/apis/raw/mediaServers";
import { KINDS_WITH_PATH_MAPPINGS } from "@/apis/raw/mediaServers";
import { CREDENTIAL_LABELS } from "@/pages/Settings/MediaServers/kinds";
import type { MediaServerDraft } from "@/pages/Setup/useOnboardingSelection";
import { validateServerUrl } from "./validation";

/** One message per field, so it lands on the field that is wrong. */
export interface DraftFieldErrors {
  name?: string;
  url?: string;
  apiKey?: string;
  pathMappings?: string;
}

export type DraftErrors = Record<string, DraftFieldErrors>;

export interface SaveOutcome {
  draftId: string;
  kind: MediaServerKind;
  name: string;
  ok: boolean;
  instanceId?: string;
  error?: string;
}

export interface SubmitResult {
  /** Non-empty means nothing was written: every draft is checked first. */
  errors: DraftErrors;
  outcomes: SaveOutcome[];
  /** The rows were written but their master switches were not flipped. */
  switchesFailed: boolean;
}

export function hasErrors(errors: DraftFieldErrors): boolean {
  return Object.values(errors).some((message) => message !== undefined);
}

/**
 * Whether a draft has been touched at all. An untouched draft is the reader
 * ticking a kind and changing their mind, and writes nothing.
 */
export function isDraftTouched(draft: MediaServerDraft): boolean {
  return (
    draft.url.trim().length > 0 ||
    draft.apiKey.trim().length > 0 ||
    draft.pathMappings.some(
      (mapping) => mapping.local_path.trim() || mapping.remote_path.trim(),
    )
  );
}

export function validateDraft(draft: MediaServerDraft): DraftFieldErrors {
  const errors: DraftFieldErrors = {};
  if (!draft.name.trim()) {
    errors.name = "Name is required";
  }
  const urlProblem = validateServerUrl(draft.url.trim());
  if (urlProblem) {
    errors.url = urlProblem;
  }
  if (!draft.apiKey.trim()) {
    errors.apiKey = `${CREDENTIAL_LABELS[draft.kind]} is required`;
  }
  if (KINDS_WITH_PATH_MAPPINGS.includes(draft.kind)) {
    const filled = draft.pathMappings.filter(
      (mapping) => mapping.local_path.trim() || mapping.remote_path.trim(),
    );
    if (filled.length === 0) {
      errors.pathMappings = "Add at least one path mapping";
    } else if (
      filled.some(
        (mapping) => !mapping.local_path.trim() || !mapping.remote_path.trim(),
      )
    ) {
      errors.pathMappings = "Fill in both paths on every mapping, or remove it";
    }
  }
  return errors;
}

// Why one server did not save, in the backend's own words where it gave any.
// A failure the reader cannot name is a failure they cannot act on.
export function describeSaveError(reason: unknown): string {
  if (reason instanceof AxiosError) {
    const data = reason.response?.data as { message?: string } | undefined;
    if (data?.message) {
      return data.message;
    }
  }
  if (reason instanceof Error && reason.message) {
    return reason.message;
  }
  return "The save failed and Bazarr+ gave no reason.";
}

function toPayload(draft: MediaServerDraft): SaveMediaServerInput {
  return {
    kind: draft.kind,
    name: draft.name.trim(),
    enabled: true,
    url: draft.url.trim(),
    api_key: draft.apiKey.trim(),
    verify_ssl: draft.verifySsl,
    path_mappings: draft.pathMappings.filter(
      (mapping) => mapping.local_path.trim() && mapping.remote_path.trim(),
    ),
    options: draft.options,
  };
}

/**
 * Writes a set of media server drafts.
 *
 * Every draft is validated before any of them is submitted, so a run never
 * writes half a selection because the last row had a typo. The creates then go
 * out together and are settled individually: one refused server leaves the
 * others saved, and the caller is told which is which so a retry can be scoped
 * to the ones that failed.
 *
 * The master switches are derived from the rows that actually landed and go in
 * one settings write, because the settings API takes a whole object. Plex is
 * never in it: its connection comes from the account flow, and
 * media_servers.plex_account sets use_plex itself, so writing it here would be
 * a second write of a switch the callback already set.
 */
export function useMediaServerSubmit() {
  const save = useSaveMediaServerInstance();
  const settings = useSettingsMutation();
  const [isPending, setPending] = useState(false);

  const submit = useCallback(
    async (
      drafts: MediaServerDraft[],
      // Called for every server that landed, before the master switches are
      // written. The caller records the row on its draft there rather than
      // waiting for the whole run, because the switch write is a second round
      // trip and the reader can be back on this step pressing Connect again
      // long before it answers.
      onCreated?: (outcome: SaveOutcome) => void,
    ): Promise<SubmitResult> => {
      const errors: DraftErrors = {};
      for (const draft of drafts) {
        const found = validateDraft(draft);
        if (hasErrors(found)) {
          errors[draft.draftId] = found;
        }
      }
      if (Object.keys(errors).length > 0) {
        return { errors, outcomes: [], switchesFailed: false };
      }

      setPending(true);
      try {
        const settled = await Promise.allSettled(
          drafts.map((draft) => save.mutateAsync(() => toPayload(draft))),
        );
        const outcomes: SaveOutcome[] = drafts.map((draft, index) => {
          const result = settled[index];
          if (result.status === "fulfilled") {
            return {
              draftId: draft.draftId,
              kind: draft.kind,
              name: draft.name.trim(),
              ok: true,
              instanceId: result.value.id,
            };
          }
          return {
            draftId: draft.draftId,
            kind: draft.kind,
            name: draft.name.trim(),
            ok: false,
            error: describeSaveError(result.reason),
          };
        });

        for (const outcome of outcomes) {
          if (outcome.ok) {
            onCreated?.(outcome);
          }
        }

        const kinds = new Set(
          outcomes
            .filter((outcome) => outcome.ok && outcome.kind !== "plex")
            .map((outcome) => outcome.kind),
        );
        let switchesFailed = false;
        if (kinds.size > 0) {
          // mutateAsync, not mutate with callbacks. TanStack drops a mutate
          // call's own onSuccess and onError once the component that made the
          // call has unmounted, which pressing Back or Skip during the save
          // does. The promise then never settled: the caller's then never ran,
          // the draft was never marked saved, its step stayed in the wizard,
          // and submitting it again wrote a second row for a server that was
          // already there. mutateAsync settles either way.
          switchesFailed = await settings
            .mutateAsync(
              Object.fromEntries(
                [...kinds].map((kind) => [
                  `settings-general-use_${kind}`,
                  true,
                ]),
              ),
            )
            .then(() => false)
            .catch(() => true);
        }
        return { errors: {}, outcomes, switchesFailed };
      } finally {
        setPending(false);
      }
    },
    [save, settings],
  );

  return { submit, isPending };
}
