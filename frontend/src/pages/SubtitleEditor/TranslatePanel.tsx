import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faTimes, faSpinner } from "@fortawesome/free-solid-svg-icons";
import {
  useTranslatorStatus,
  useTranslatorJob,
  useCancelTranslatorJob,
  useTranslatorModels,
} from "@/apis/hooks/translator";
import { useLanguages } from "@/apis/hooks/languages";
import client from "@/apis/raw/client";

interface TranslatePanelProps {
  open: boolean;
  cues: Array<{ id: string; text: string }>;
  referenceCues?: Array<{ startMs: number; endMs: number; text: string }>;
  availableSubtitles?: Array<{ language: string; format: string }>;
  mediaType?: string;
  mediaId?: number;
  currentLanguage: string;
  mediaTitle?: string;
  onApplyTranslation: (translations: Map<number, string>) => void;
  onSetReference: (texts: Map<number, string>) => void;
  onTranslatingChange?: (active: boolean) => void;
  onLoadReference: (language: string) => void;
  onImportReference: () => void;
  referenceLanguage?: string;
  onClose: () => void;
}

type TranslatePhase =
  | "idle"
  | "submitting"
  | "translating"
  | "completed"
  | "failed";

const styles = {
  container: {
    position: "absolute",
    top: 8,
    right: 8,
    zIndex: 10,
    minWidth: 400,
    maxWidth: 480,
    background: "var(--bz-surface-raised)",
    border: "1px solid var(--bz-border-card)",
    borderRadius: "var(--bz-radius-sm)",
    padding: "10px 14px",
    boxShadow: "var(--bz-shadow-float)",
    display: "flex",
    flexDirection: "column",
    gap: 8,
  } satisfies CSSProperties,

  row: {
    display: "flex",
    alignItems: "center",
    gap: 6,
  } satisfies CSSProperties,

  label: {
    fontSize: 12,
    color: "var(--bz-text-secondary)",
    flexShrink: 0,
    minWidth: 50,
  } satisfies CSSProperties,

  select: {
    flex: 1,
    fontFamily: "monospace",
    fontSize: 13,
    background: "var(--bz-surface-base)",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-primary)",
    borderRadius: 4,
    padding: "5px 8px",
    outline: "none",
    appearance: "auto" as const,
  } satisfies CSSProperties,

  btn: {
    background: "transparent",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-secondary)",
    borderRadius: 4,
    padding: "4px 10px",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    flexShrink: 0,
  } satisfies CSSProperties,

  btnPrimary: {
    background: "var(--mantine-color-brand-5)",
    border: "1px solid var(--mantine-color-brand-5)",
    color: "#fff",
    borderRadius: 4,
    padding: "5px 14px",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    flexShrink: 0,
    fontWeight: 600,
  } satisfies CSSProperties,

  btnSuccess: {
    background: "#059669",
    border: "1px solid #059669",
    color: "#fff",
    borderRadius: 4,
    padding: "5px 14px",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    flexShrink: 0,
    fontWeight: 600,
  } satisfies CSSProperties,

  btnDanger: {
    background: "#dc2626",
    border: "1px solid #dc2626",
    color: "#fff",
    borderRadius: 4,
    padding: "4px 10px",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    flexShrink: 0,
  } satisfies CSSProperties,

  progressBar: {
    height: 6,
    borderRadius: 3,
    background: "var(--bz-surface-base)",
    overflow: "hidden",
    width: "100%",
  } satisfies CSSProperties,

  progressFill: {
    height: "100%",
    borderRadius: 3,
    background: "var(--mantine-color-brand-5)",
    transition: "width 0.3s ease",
  } satisfies CSSProperties,

  statusText: {
    fontSize: 11,
    color: "var(--bz-text-tertiary)",
  } satisfies CSSProperties,

  errorText: {
    fontSize: 11,
    color: "#f87171",
    lineHeight: 1.4,
  } satisfies CSSProperties,

  successText: {
    fontSize: 11,
    color: "#34d399",
  } satisfies CSSProperties,

  infoText: {
    fontSize: 11,
    color: "#fbbf24",
    lineHeight: 1.4,
  } satisfies CSSProperties,
};

interface SubmitResponse {
  jobId: string;
}

async function submitTranslationJob(params: {
  lines: Array<{ position: number; line: string }>;
  sourceLanguage: string;
  targetLanguage: string;
  title: string;
  mediaType: string;
}): Promise<SubmitResponse> {
  const response = await client.axios.post<SubmitResponse>(
    "/translator/jobs",
    params,
  );
  return response.data;
}

export default function TranslatePanel({
  open,
  cues,
  referenceCues,
  availableSubtitles,
  currentLanguage,
  mediaTitle,
  onApplyTranslation,
  onSetReference,
  onTranslatingChange,
  onLoadReference,
  onImportReference,
  referenceLanguage,
  onClose,
}: TranslatePanelProps) {
  const [sourceLanguage, setSourceLanguage] = useState("");
  const [targetLanguage, setTargetLanguage] = useState("");
  const [phase, setPhase] = useState<TranslatePhase>("idle");
  const [jobId, setJobId] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [translatedLines, setTranslatedLines] = useState<
    Map<number, string>
  >(new Map());

  const [applied, setApplied] = useState(false);

  const { data: translatorStatus } = useTranslatorStatus(open);
  const { data: modelsData } = useTranslatorModels(open);
  const { data: serverLanguages } = useLanguages();
  const cancelJob = useCancelTranslatorJob();

  const isTranslatorAvailable =
    translatorStatus?.healthy === true &&
    translatorStatus?.config?.apiKeyConfigured === true;

  const languageOptions = useMemo(() => {
    if (!serverLanguages) return [];
    return serverLanguages
      .filter((l) => l.enabled)
      .map((l) => ({ value: l.code2, label: l.name }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [serverLanguages]);

  // Default target language to current editor language
  useEffect(() => {
    if (open && currentLanguage && !targetLanguage) {
      setTargetLanguage(currentLanguage);
    }
  }, [open, currentLanguage, targetLanguage]);

  // Poll the active job
  const { data: jobData } = useTranslatorJob(
    phase === "translating" ? jobId : "",
  );

  // React to job status changes
  useEffect(() => {
    if (phase !== "translating" || !jobData) return;

    if (jobData.status === "completed" || jobData.status === "partial") {
      const result = jobData.result as
        | { lines?: Array<{ position: number; line: string }> }
        | undefined;
      const lines = result?.lines;
      if (lines && Array.isArray(lines)) {
        const map = new Map<number, string>();
        for (const item of lines) {
          map.set(item.position, item.line);
        }
        setTranslatedLines(map);
        // Only set reference when translating from editor cues, not from loaded reference
        if (!referenceCues || referenceCues.length === 0) {
          onSetReference(map);
        }
        setPhase("completed");
        onTranslatingChange?.(false);
        setApplied(false);
      } else {
        setPhase("failed");
        onTranslatingChange?.(false);
        setErrorMsg("Translation completed but no lines were returned.");
      }
    } else if (jobData.status === "failed") {
      setPhase("failed");
      onTranslatingChange?.(false);
      setErrorMsg(jobData.error || jobData.message || "Translation failed.");
    } else if (jobData.status === "cancelled") {
      setPhase("idle");
      onTranslatingChange?.(false);
      setJobId("");
    }
  }, [phase, jobData]);

  const handleSubmit = useCallback(async () => {
    if (!targetLanguage) return;
    // Use reference cues as source when available, otherwise use editor cues
    const sourceLines = referenceCues && referenceCues.length > 0
      ? referenceCues.map((r, i) => ({ position: i, line: r.text }))
      : cues.map((cue, i) => ({ position: i, line: cue.text }));
    if (sourceLines.length === 0) return;

    setPhase("submitting");
    setErrorMsg("");
    setTranslatedLines(new Map());
    setApplied(false);

    const lines = sourceLines;

    // Resolve language codes to full names for better translation
    const sourceLangName =
      languageOptions.find((l) => l.value === sourceLanguage)?.label ||
      sourceLanguage;
    const targetLangName =
      languageOptions.find((l) => l.value === targetLanguage)?.label ||
      targetLanguage;

    try {
      const result = await submitTranslationJob({
        lines,
        sourceLanguage: sourceLangName,
        targetLanguage: targetLangName,
        title: mediaTitle || "",
        mediaType: "",
      });

      if (result.jobId) {
        setJobId(result.jobId);
        setPhase("translating");
        onTranslatingChange?.(true);
      } else {
        setPhase("failed");
        setErrorMsg("No job ID returned from translator.");
      }
    } catch (err) {
      setPhase("failed");
      onTranslatingChange?.(false);
      setErrorMsg(
        (err as any)?.response?.data?.error ||
          (err as Error).message ||
          "Failed to submit translation job.",
      );
    }
  }, [cues, referenceCues, sourceLanguage, targetLanguage, mediaTitle, languageOptions]);

  const handleCancel = useCallback(() => {
    if (jobId) {
      cancelJob.mutate(jobId);
    }
    setPhase("idle");
    setJobId("");
  }, [jobId, cancelJob]);

  const handleApply = useCallback(() => {
    if (translatedLines.size === 0 || applied) return;
    setApplied(true);
    onApplyTranslation(translatedLines);
  }, [translatedLines, onApplyTranslation]);

  const handleReset = useCallback(() => {
    setPhase("idle");
    setJobId("");
    setErrorMsg("");
    setTranslatedLines(new Map());
    setApplied(false);
  }, []);

  if (!open) return null;

  const progress = jobData?.progress ?? 0;
  const jobMessage = jobData?.message || "";
  const completedLines = jobData?.completedLines ?? 0;
  const totalLines = jobData?.totalLines ?? cues.length;
  const modelUsed = modelsData?.default_model || "";
  const isWorking = phase === "submitting" || phase === "translating";

  return (
    <div style={styles.container}>
      {/* Header row */}
      <div style={{ ...styles.row, justifyContent: "space-between" }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--bz-text-primary)" }}>
          AI Translate
        </span>
        <button
          type="button"
          style={styles.btn}
          onClick={onClose}
          title="Close (Escape)"
        >
          <FontAwesomeIcon icon={faTimes} />
        </button>
      </div>

      {/* Reference subtitle loader */}
      <div style={{ ...styles.row, gap: 6 }}>
        <span style={styles.label}>Reference</span>
        <select
          style={{ ...styles.select, flex: 1 }}
          value={referenceLanguage ?? ""}
          onChange={(e) => {
            if (e.target.value) onLoadReference(e.target.value);
          }}
          disabled={isWorking}
        >
          <option value="">Load existing subtitle...</option>
          {availableSubtitles && availableSubtitles.length > 0
            ? availableSubtitles
                .filter((s) => s.language !== currentLanguage)
                .map((s) => {
                  const langName = (serverLanguages ?? []).find((l) => l.code2 === s.language.split(":")[0])?.name ?? s.language;
                  const modifier = s.language.includes(":") ? ` (${s.language.split(":")[1].toUpperCase()})` : "";
                  return (
                    <option key={s.language} value={s.language}>
                      {langName}{modifier} [{s.format}]
                    </option>
                  );
                })
            : languageOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))
          }
        </select>
        <button
          type="button"
          style={styles.btn}
          onClick={onImportReference}
          disabled={isWorking}
          title="Import from file"
        >
          Import
        </button>
      </div>

      <div style={{ borderTop: "1px solid var(--bz-border-divider)", margin: "4px 0" }} />

      {/* Source language */}
      <div style={styles.row}>
        <span style={styles.label}>From</span>
        <select
          style={styles.select}
          value={sourceLanguage}
          onChange={(e) => setSourceLanguage(e.target.value)}
          disabled={isWorking}
        >
          <option value="">Auto-detect</option>
          {languageOptions.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* Target language */}
      <div style={styles.row}>
        <span style={styles.label}>To</span>
        <select
          style={styles.select}
          value={targetLanguage}
          onChange={(e) => setTargetLanguage(e.target.value)}
          disabled={isWorking}
        >
          <option value="" disabled>
            Select language...
          </option>
          {languageOptions.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* Model info */}
      {modelUsed && (
        <div style={styles.statusText}>Model: {modelUsed}</div>
      )}

      {/* Cue count */}
      <div style={styles.statusText}>{referenceCues && referenceCues.length > 0 ? referenceCues.length : cues.length} cues to translate{referenceCues && referenceCues.length > 0 ? " (from reference)" : ""}</div>

      {/* Progress section */}
      {phase === "translating" && (
        <>
          <div style={styles.progressBar}>
            <div
              style={{ ...styles.progressFill, width: `${progress}%` }}
            />
          </div>
          <div style={styles.statusText}>
            {jobMessage ||
              `Translating... ${completedLines}/${totalLines} lines (${Math.round(progress)}%)`}
          </div>
        </>
      )}

      {/* Error */}
      {phase === "failed" && (
        <div style={styles.errorText}>{errorMsg}</div>
      )}

      {/* Success */}
      {phase === "completed" && (
        <div style={styles.successText}>
          Translation complete. {translatedLines.size}/{referenceCues && referenceCues.length > 0 ? referenceCues.length : cues.length} lines
          translated.
          {translatedLines.size < (referenceCues && referenceCues.length > 0 ? referenceCues.length : cues.length) && (
            <span style={{ color: "#fbbf24" }}>
              {" "}
              {cues.length - translatedLines.size} lines were not translated.
            </span>
          )}
        </div>
      )}

      {/* Action buttons */}
      <div style={{ ...styles.row, justifyContent: "flex-end", marginTop: 2 }}>
        {phase === "idle" && (
          <button
            type="button"
            style={{
              ...styles.btnPrimary,
              ...(((!referenceCues || referenceCues.length === 0) && cues.length === 0) || !targetLanguage
                ? { opacity: 0.5, cursor: "not-allowed" }
                : {}),
            }}
            disabled={
              ((!referenceCues || referenceCues.length === 0) && cues.length === 0) ||
              !targetLanguage
            }
            onClick={handleSubmit}
          >
            Translate
          </button>
        )}

        {phase === "submitting" && (
          <button
            type="button"
            style={{ ...styles.btnPrimary, opacity: 0.7, cursor: "wait" }}
            disabled
          >
            <FontAwesomeIcon icon={faSpinner} spin />{" "}
            Submitting...
          </button>
        )}

        {phase === "translating" && (
          <button
            type="button"
            style={styles.btnDanger}
            onClick={handleCancel}
          >
            Cancel
          </button>
        )}

        {phase === "completed" && (
          <>
            <button
              type="button"
              style={styles.btn}
              onClick={handleReset}
            >
              Reset
            </button>
            <button
              type="button"
              style={{
                ...styles.btnSuccess,
                ...(applied
                  ? { opacity: 0.5, cursor: "not-allowed" }
                  : {}),
              }}
              disabled={applied}
              onClick={handleApply}
            >
              {applied ? "Applied" : "Apply to Cues"}
            </button>
          </>
        )}

        {phase === "failed" && (
          <button
            type="button"
            style={styles.btn}
            onClick={handleReset}
          >
            Try Again
          </button>
        )}
      </div>
    </div>
  );
}
