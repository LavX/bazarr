import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { Link, useBlocker, useNavigate, useParams } from "react-router";
import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Center,
  Group,
  Loader,
  Menu,
  Modal,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { useLanguages } from "@/apis/hooks/languages";
import {
  useSubtitleContent,
  useSubtitleCreate,
  useSubtitleSave,
} from "@/apis/hooks/subtitles";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import client from "@/apis/raw/client";
import { Environment } from "@/utilities/env";
import { isCombinedOutputLanguageKey } from "@/utilities/subtitles";
import DetailPane, { type DetailPaneHandle } from "./DetailPane";
import {
  computeCueWarnings,
  createAddCue,
  createDeleteCues,
  createEditText,
  createEditTiming,
  createInitialDocumentState,
  createMergeCues,
  createSplitCue,
  type CueWarning,
  DEFAULT_QC_PRESET,
  detectGaps,
  type QCPreset,
  subtitleDocumentReducer,
} from "./document";
import EditableCueTable from "./EditableCueTable";
import EditorToolbar from "./EditorToolbar";
import JumpToCue from "./JumpToCue";
import { detectFormat, getParser } from "./parsers";
import QCPanel from "./QCPanel";
import SearchReplace from "./SearchReplace";
import { getSerializer } from "./serializers";
import ShortcutSheet from "./ShortcutSheet";
import StatusBar from "./StatusBar";
import TimingToolsPanel from "./TimingToolsPanel";
import TranslatePanel from "./TranslatePanel";
import type { Cue, ParseResult, SubtitleFormat } from "./types";
import type { VideoPreviewHandle } from "./VideoPreview";
import VideoPreview from "./VideoPreview";
import WaveformTimeline from "./WaveformTimeline";

export function findActiveCueIndex(cues: Cue[], playbackTimeMs: number) {
  return cues.findIndex(
    (cue) => playbackTimeMs >= cue.startMs && playbackTimeMs <= cue.endMs,
  );
}

export function getActiveCueText(cues: Cue[], playbackTimeMs: number) {
  const index = findActiveCueIndex(cues, playbackTimeMs);
  return index >= 0 ? cues[index].text : undefined;
}

export default function EditorPage() {
  const { mediaType, mediaId, language } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Fetch subtitle content
  const {
    data: queryResult,
    isLoading,
    error,
  } = useSubtitleContent(
    mediaType,
    mediaId ? Number(mediaId) : undefined,
    language,
  );

  const data = queryResult?.data;
  const [createdSuccessfully, setCreatedSuccessfully] = useState(false);
  const isNewSubtitle = data?.exists === false && !createdSuccessfully;

  // Parse the fetched content
  const parseResult = useMemo<ParseResult | null>(() => {
    if (!data) return null;
    try {
      return getParser(data.format as SubtitleFormat).parse(data.content);
    } catch {
      return null;
    }
  }, [data]);

  // Combined outputs (en:combined-hu) stack languages at shared timestamps. For
  // ASS that means one positioned cue per language, so we suppress the QC
  // overlap warning for them instead of treating the stack as an error.
  const isCombined = isCombinedOutputLanguageKey(language);

  // Format derived from data
  const format = (data?.format as SubtitleFormat) ?? "srt";
  const encoding = data?.encoding ?? "utf-8";

  // Document state
  const [docState, dispatch] = useReducer(
    subtitleDocumentReducer,
    [],
    createInitialDocumentState,
  );

  // Track whether we've loaded data into the reducer
  const loadedRef = useRef(false);
  const etagRef = useRef<string>("");

  // Reset when switching to a different subtitle
  const prevLangRef = useRef(language);
  useEffect(() => {
    if (prevLangRef.current !== language) {
      loadedRef.current = false;
      setCreatedSuccessfully(false);
      prevLangRef.current = language;
    }
  }, [language]);

  // Load cues into document state when data arrives
  useEffect(() => {
    if (parseResult && !loadedRef.current) {
      dispatch({ type: "LOAD", cues: parseResult.cues });
      loadedRef.current = true;
    }
  }, [parseResult]);

  // Auto-save to localStorage on every change (debounced 2s)
  const autoSaveKey =
    mediaType && mediaId && language
      ? `bazarr-editor-${mediaType}-${mediaId}-${language}`
      : null;

  useEffect(() => {
    if (!autoSaveKey || docState.cues.length === 0) return;
    const timer = setTimeout(() => {
      try {
        const serialized = getSerializer(format).serialize({
          metadata: parseResult?.metadata ?? { format },
          cues: docState.cues,
        });
        localStorage.setItem(
          autoSaveKey,
          JSON.stringify({
            content: serialized,
            format,
            cueCount: docState.cues.length,
            timestamp: Date.now(),
          }),
        );
      } catch {
        /* quota exceeded */
      }
    }, 2000);
    return () => clearTimeout(timer);
  }, [autoSaveKey, docState.cues, format, parseResult?.metadata]);

  // Auto-save recovery: check on load if there's a newer draft in localStorage
  const [recoveryAvailable, setRecoveryAvailable] = useState<{
    content: string;
    format: string;
    cueCount: number;
    timestamp: number;
  } | null>(null);

  useEffect(() => {
    if (!autoSaveKey || !loadedRef.current) return;
    try {
      const raw = localStorage.getItem(autoSaveKey);
      if (!raw) return;
      const saved = JSON.parse(raw);
      const age = Date.now() - saved.timestamp;
      if (age < 86400000 && saved.cueCount > 0) {
        // Less than 24 hours old
        setRecoveryAvailable(saved);
      } else {
        localStorage.removeItem(autoSaveKey);
      }
    } catch {
      localStorage.removeItem(autoSaveKey!);
    }
  }, [autoSaveKey]);

  const handleRecoverDraft = useCallback(() => {
    if (!recoveryAvailable) return;
    try {
      const result = getParser(
        recoveryAvailable.format as SubtitleFormat,
      ).parse(recoveryAvailable.content);
      dispatch({ type: "LOAD", cues: result.cues });
      dispatch({ type: "MARK_DIRTY" });
    } catch {
      /* ignore */
    }
    setRecoveryAvailable(null);
    if (autoSaveKey) localStorage.removeItem(autoSaveKey);
  }, [recoveryAvailable, autoSaveKey]);

  const handleDismissRecovery = useCallback(() => {
    setRecoveryAvailable(null);
    if (autoSaveKey) localStorage.removeItem(autoSaveKey);
  }, [autoSaveKey]);

  // Keep etagRef in sync whenever the query result changes (initial load,
  // refetch after POST create, etc.). Backend sends ETag with quotes,
  // e.g. '"abc123"'. Strip them so the If-Match header works correctly.
  useEffect(() => {
    if (queryResult?.etag) {
      etagRef.current = queryResult.etag.replace(/^"|"$/g, "");
    }
  }, [queryResult?.etag]);

  // Selection state
  const [selectedIndex, setSelectedIndex] = useState(0);
  const selectedCueIdRef = useRef<string | null>(null);
  const [multiSelect, setMultiSelect] = useState<Set<number>>(new Set());
  const [searchOpen, setSearchOpen] = useState(false);
  const [jumpOpen, setJumpOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [qcOpen, setQcOpen] = useState(false);
  const [timingOpen, setTimingOpen] = useState(false);
  const [translateOpen, setTranslateOpen] = useState(false);
  const [translating, setTranslating] = useState(false);
  const [qcPreset, setQcPreset] = useState<QCPreset>(DEFAULT_QC_PRESET);

  // Bookmark state (UI-only, not persisted to file)
  const [bookmarkedIds, setBookmarkedIds] = useState<Set<string>>(new Set());
  const [bookmarkFilterActive, setBookmarkFilterActive] = useState(false);

  // Reference subtitle for side-by-side view
  const [referenceCueList, setReferenceCueList] = useState<
    Array<{ startMs: number; endMs: number; text: string }>
  >([]);
  const [referenceLanguage, setReferenceLanguage] = useState<
    string | undefined
  >();
  const [referenceOpen, setReferenceOpen] = useState(false);
  const [translatingLineIdx, setTranslatingLineIdx] = useState<number | null>(
    null,
  );
  const refFileInputRef = useRef<HTMLInputElement>(null);

  // Audio track selection
  const [audioTracks, setAudioTracks] = useState<
    Array<{
      index: number;
      codec: string;
      language: string;
      title: string;
      channels: number;
      label: string;
    }>
  >([]);
  const [selectedAudioTrack, setSelectedAudioTrack] = useState(0);

  // Available subtitles for this media (for reference subtitle dropdown)
  const [availableSubtitles, setAvailableSubtitles] = useState<
    Array<{ language: string; format: string }>
  >([]);
  useEffect(() => {
    if (!mediaType || !mediaId) return;
    const apiKey = Environment.apiKey ?? "";
    fetch(
      `${Environment.baseUrl}/api/editor/subtitles?mediaType=${mediaType}&mediaId=${mediaId}&apikey=${encodeURIComponent(apiKey)}`,
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.subtitles) setAvailableSubtitles(data.subtitles);
      })
      .catch(() => {
        /* ignored */
      });
  }, [mediaType, mediaId]);

  const toggleBookmark = useCallback((cueId: string) => {
    setBookmarkedIds((prev) => {
      const next = new Set(prev);
      if (next.has(cueId)) {
        next.delete(cueId);
      } else {
        next.add(cueId);
      }
      return next;
    });
  }, []);

  // Save mutations
  const saveMutation = useSubtitleSave();
  const createMutation = useSubtitleCreate();

  // Save As modal state
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [saveAsLang, setSaveAsLang] = useState<string | null>(null);

  // Available languages for Save As dropdown
  const { data: serverLanguages } = useLanguages();
  const languageOptions = useMemo(
    () =>
      (serverLanguages ?? [])
        .filter((l) => l.enabled)
        .map((l) => ({ value: l.code2, label: `${l.name} (${l.code2})` })),
    [serverLanguages],
  );

  // Total duration of all cues
  const totalDurationMs = useMemo(
    () =>
      docState.cues.reduce(
        (sum, c) => sum + Math.max(0, c.endMs - c.startMs),
        0,
      ),
    [docState.cues],
  );

  // Warnings
  const warnings = useMemo(() => {
    const map = new Map<number, CueWarning>();
    docState.cues.forEach((cue, i) => {
      const w = computeCueWarnings(
        cue,
        docState.cues[i - 1] ?? null,
        docState.cues[i + 1] ?? null,
        qcPreset,
        isCombined,
      );
      if (w) map.set(i, w);
    });
    return map;
  }, [docState.cues, qcPreset, isCombined]);

  // Ghost cues (gap detection)
  const ghostCues = useMemo(
    () => detectGaps(docState.cues, 1000),
    [docState.cues],
  );

  // Track selected cue by id so selection survives re-sorting
  const curSelectedCue =
    selectedIndex >= 0 && selectedIndex < docState.cues.length
      ? docState.cues[selectedIndex]
      : null;
  if (curSelectedCue) selectedCueIdRef.current = curSelectedCue.id;
  useEffect(() => {
    if (!selectedCueIdRef.current || docState.cues.length === 0) return;
    const cueAtIndex =
      selectedIndex >= 0 && selectedIndex < docState.cues.length
        ? docState.cues[selectedIndex]
        : null;
    if (cueAtIndex && cueAtIndex.id === selectedCueIdRef.current) return;
    const newIdx = docState.cues.findIndex(
      (c) => c.id === selectedCueIdRef.current,
    );
    if (newIdx >= 0 && newIdx !== selectedIndex) {
      setSelectedIndex(newIdx);
    }
  }, [docState.cues, selectedIndex]);

  // Navigation blocker for unsaved changes
  const blocker = useBlocker(docState.dirty);

  // Browser beforeunload warning for unsaved changes
  useEffect(() => {
    if (!docState.dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [docState.dirty]);

  // File input ref for upload
  const fileInputRef = useRef<HTMLInputElement>(null);
  const cursorPosRef = useRef<number>(0);
  const detailPaneRef = useRef<DetailPaneHandle>(null);
  const videoPreviewRef = useRef<VideoPreviewHandle>(null);
  const [, setVideoPlaying] = useState(false);
  const [userSeekMs, setUserSeekMs] = useState<number | null>(null);
  const [seekCounter, setSeekCounter] = useState(0);
  const [playbackTimeMs, setPlaybackTimeMs] = useState(0);
  const [shouldFocusTextarea, setShouldFocusTextarea] = useState(false);

  // Build a ParseResult from current state for serialization
  const buildParseResult = useCallback((): ParseResult => {
    return {
      metadata: parseResult?.metadata ?? { format },
      cues: docState.cues,
    };
  }, [docState.cues, parseResult?.metadata, format]);

  // Save handler
  const handleSave = useCallback(() => {
    if (!mediaType || !mediaId || !language) return;

    // Commit any uncommitted text from the detail pane textarea before
    // serializing. This handles the case where the user typed text and
    // hit Ctrl+S without blurring the textarea.
    const uncommitted = detailPaneRef.current?.getUncommittedText();
    if (
      uncommitted !== null &&
      uncommitted !== undefined &&
      selectedIndex >= 0 &&
      selectedIndex < docState.cues.length
    ) {
      dispatch({
        type: "APPLY_OP",
        op: createEditText(selectedIndex, uncommitted),
      });
    }

    // Use a microtask to let the dispatch above settle before serializing.
    // The reducer is synchronous so docState updates in the same render,
    // but we need the updated cues. Instead, build the parse result manually
    // with the patched cue if needed.
    const cuesForSave =
      uncommitted !== null && uncommitted !== undefined
        ? docState.cues.map((c, i) =>
            i === selectedIndex ? { ...c, text: uncommitted } : c,
          )
        : docState.cues;

    const serialized = getSerializer(format).serialize({
      metadata: parseResult?.metadata ?? { format },
      cues: cuesForSave,
    });

    if (isNewSubtitle) {
      // New subtitle: use POST to create
      createMutation.mutate(
        {
          mediaType,
          mediaId: Number(mediaId),
          content: serialized,
          language,
          format,
          forced: false,
          hi: false,
        },
        {
          onSuccess: () => {
            dispatch({ type: "MARK_SAVED" });
            setCreatedSuccessfully(true); // Switch from create to edit mode
            if (autoSaveKey) {
              try {
                localStorage.removeItem(autoSaveKey);
              } catch {
                /* ignore */
              }
            }
            // Refetch content so we pick up the etag and confirm the file
            // is indexed in the DB (needed for subsequent PUT saves).
            queryClient.invalidateQueries({
              queryKey: [
                QueryKeys.Subtitles,
                "content",
                mediaType,
                Number(mediaId),
                language,
              ],
            });
            showNotification({
              message: "Subtitle created",
              color: "green",
              autoClose: 2000,
            });
          },
          onError: (err) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const status = (err as any)?.response?.status;
            const msg =
              status === 409
                ? "Subtitle file already exists for this language."
                : `Create failed: ${(err as Error).message}`;
            showNotification({
              title: "Error",
              message: msg,
              color: "red",
              autoClose: 5000,
            });
          },
        },
      );
    } else {
      // Existing subtitle: use PUT to update
      saveMutation.mutate(
        {
          mediaType,
          mediaId: Number(mediaId),
          language,
          content: serialized,
          encoding,
          etag: etagRef.current || undefined,
        },
        {
          onSuccess: (result) => {
            dispatch({ type: "MARK_SAVED" });
            if (result?.etag) {
              // Strip surrounding quotes from the ETag header value
              etagRef.current = result.etag.replace(/^"|"$/g, "");
            }
            if (autoSaveKey) {
              try {
                localStorage.removeItem(autoSaveKey);
              } catch {
                /* ignore */
              }
            }
            showNotification({
              message: "Saved",
              color: "green",
              autoClose: 2000,
            });
          },
          onError: (err) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const status = (err as any)?.response?.status;
            if (status === 412) {
              // ETag mismatch: file was modified externally.
              // Force save by retrying without ETag (skip conflict check).
              saveMutation.mutate(
                {
                  mediaType,
                  mediaId: Number(mediaId),
                  language,
                  content: serialized,
                  encoding,
                  etag: undefined,
                },
                {
                  onSuccess: (result) => {
                    dispatch({ type: "MARK_SAVED" });
                    if (result?.etag) {
                      etagRef.current = result.etag.replace(/^"|"$/g, "");
                    }
                    if (autoSaveKey) {
                      try {
                        localStorage.removeItem(autoSaveKey);
                      } catch {
                        /* ignore */
                      }
                    }
                    showNotification({
                      message: "Saved (overwrite)",
                      color: "yellow",
                      autoClose: 2000,
                    });
                  },
                  onError: () => {
                    showNotification({
                      title: "Save Error",
                      message: "Save failed even after retry.",
                      color: "red",
                      autoClose: 5000,
                    });
                  },
                },
              );
              return;
            }
            const msg =
              status === 507
                ? "Disk full. Cannot save."
                : `Save failed: ${(err as Error).message}`;
            showNotification({
              title: "Save Error",
              message: msg,
              color: "red",
              autoClose: 5000,
            });
          },
        },
      );
    }
  }, [
    mediaType,
    mediaId,
    language,
    format,
    encoding,
    isNewSubtitle,
    saveMutation,
    createMutation,
    autoSaveKey,
    selectedIndex,
    docState.cues,
    parseResult?.metadata,
    queryClient,
  ]);

  // Subtitle language switcher
  const [pendingLangSwitch, setPendingLangSwitch] = useState<string | null>(
    null,
  );
  const switchToLanguage = useCallback(
    (targetLang: string) => {
      if (targetLang === language) return;
      if (docState.dirty) {
        setPendingLangSwitch(targetLang);
      } else {
        navigate(`/subtitles/edit/${mediaType}/${mediaId}/${targetLang}`, {
          replace: true,
        });
      }
    },
    [language, mediaType, mediaId, docState.dirty, navigate],
  );

  // Navigate after save completes (dirty becomes false while pendingLangSwitch is set)
  const pendingSaveNavRef = useRef<string | null>(null);
  useEffect(() => {
    if (pendingSaveNavRef.current && !docState.dirty) {
      const target = pendingSaveNavRef.current;
      pendingSaveNavRef.current = null;
      requestAnimationFrame(() => {
        navigate(`/subtitles/edit/${mediaType}/${mediaId}/${target}`, {
          replace: true,
        });
      });
    }
  }, [docState.dirty, mediaType, mediaId, navigate]);

  const handleSwitchSave = useCallback(() => {
    if (!pendingLangSwitch) return;
    pendingSaveNavRef.current = pendingLangSwitch;
    setPendingLangSwitch(null);
    handleSave();
  }, [pendingLangSwitch, handleSave]);

  const handleSwitchDrop = useCallback(() => {
    if (!pendingLangSwitch) return;
    dispatch({ type: "MARK_SAVED" });
    setPendingLangSwitch(null);
    // Defer navigation so React processes MARK_SAVED first (dirty=false), avoiding the blocker
    requestAnimationFrame(() => {
      navigate(`/subtitles/edit/${mediaType}/${mediaId}/${pendingLangSwitch}`, {
        replace: true,
      });
    });
  }, [pendingLangSwitch, mediaType, mediaId, navigate]);

  // Save As handler (create new subtitle with different language)
  // State for overwrite confirmation
  const [overwriteConfirm, setOverwriteConfirm] = useState<{
    lang: string;
    content: string;
  } | null>(null);

  const handleSaveAs = useCallback(
    (targetLang: string) => {
      if (!mediaType || !mediaId || !targetLang) return;
      const serialized = getSerializer(format).serialize(buildParseResult());
      createMutation.mutate(
        {
          mediaType,
          mediaId: Number(mediaId),
          content: serialized,
          language: targetLang,
          format,
          forced: false,
          hi: false,
        },
        {
          onSuccess: () => {
            setSaveAsOpen(false);
            setSaveAsLang(null);
            dispatch({ type: "MARK_SAVED" });
            requestAnimationFrame(() => {
              navigate(
                `/subtitles/edit/${mediaType}/${mediaId}/${encodeURIComponent(targetLang)}`,
                { replace: true },
              );
            });
          },
          onError: (err) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const status = (err as any)?.response?.status;
            if (status === 409) {
              // File already exists, ask to overwrite
              setOverwriteConfirm({ lang: targetLang, content: serialized });
            } else {
              showNotification({
                title: "Error",
                message: `Save As failed: ${(err as Error).message}`,
                color: "red",
                autoClose: 5000,
              });
            }
          },
        },
      );
    },
    [mediaType, mediaId, format, buildParseResult, createMutation, navigate],
  );

  const handleOverwrite = useCallback(() => {
    if (!overwriteConfirm || !mediaType || !mediaId) return;
    const { lang, content } = overwriteConfirm;
    saveMutation.mutate(
      {
        mediaType,
        mediaId: Number(mediaId),
        language: lang,
        content,
        encoding,
      },
      {
        onSuccess: (result) => {
          setOverwriteConfirm(null);
          setSaveAsOpen(false);
          setSaveAsLang(null);
          dispatch({ type: "MARK_SAVED" });
          if (result?.etag) {
            etagRef.current = result.etag.replace(/^"|"$/g, "");
          }
          showNotification({
            message: `Overwritten ${lang} subtitle`,
            color: "green",
            autoClose: 2000,
          });
          requestAnimationFrame(() => {
            navigate(
              `/subtitles/edit/${mediaType}/${mediaId}/${encodeURIComponent(lang)}`,
              { replace: true },
            );
          });
        },
        onError: (err) => {
          setOverwriteConfirm(null);
          showNotification({
            title: "Error",
            message: `Overwrite failed: ${(err as Error).message}`,
            color: "red",
            autoClose: 5000,
          });
        },
      },
    );
  }, [overwriteConfirm, mediaType, mediaId, encoding, saveMutation, navigate]);

  // Upload handler
  const handleUpload = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const handleFileSelected = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const text = reader.result as string;
        const detectedFormat = detectFormat(file.name);
        try {
          const result = getParser(detectedFormat).parse(text);
          dispatch({ type: "LOAD", cues: result.cues });
          dispatch({ type: "MARK_DIRTY" });
        } catch {
          // Parse failure, ignore
        }
      };
      reader.readAsText(file);
      // Reset so re-selecting the same file triggers change
      e.target.value = "";
    },
    [],
  );

  // Download handler
  const handleDownload = useCallback(() => {
    const serialized = getSerializer(format).serialize(buildParseResult());
    const blob = new Blob([serialized], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const title =
      data?.mediaTitle?.replace(/[/\\?%*:|"<>]/g, "_") ?? "subtitle";
    const langPart = data?.language ?? language ?? "unknown";
    a.download = `${title}.${langPart}.${format}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [format, language, data?.mediaTitle, data?.language, buildParseResult]);

  // Toolbar action handler
  const handleToolbarAction = useCallback(
    (action: string) => {
      switch (action) {
        case "save":
          handleSave();
          break;
        case "upload":
          handleUpload();
          break;
        case "download":
          handleDownload();
          break;
        case "saveAs":
          setSaveAsOpen(true);
          break;
        case "undo":
          dispatch({ type: "UNDO" });
          break;
        case "redo":
          dispatch({ type: "REDO" });
          break;
        case "search":
          setSearchOpen((v) => !v);
          break;
        case "qc":
          setQcOpen((v) => !v);
          break;
        case "timing":
          setTimingOpen((v) => !v);
          break;
        case "translate":
          setTranslateOpen((v) => !v);
          break;
        case "reference":
          setReferenceOpen((v) => !v);
          break;
        case "addCue": {
          const currentPos = playbackTimeMs;
          const startMs =
            currentPos > 0
              ? currentPos
              : docState.cues.length > 0
                ? docState.cues[Math.max(0, selectedIndex)].endMs + 100
                : 0;
          const newCue: Cue = {
            id: crypto.randomUUID(),
            startMs,
            endMs: startMs + 3000,
            text: "",
          };
          // Insert at the right position based on time
          let insertIdx = docState.cues.findIndex((c) => c.startMs > startMs);
          if (insertIdx === -1) insertIdx = docState.cues.length;
          // addCue inserts after afterIdx, so we need afterIdx = insertIdx - 1
          dispatch({
            type: "APPLY_OP",
            op: createAddCue(insertIdx - 1, newCue),
          });
          setSelectedIndex(insertIdx);
          setShouldFocusTextarea(true);
          break;
        }
        case "delete": {
          const indices =
            multiSelect.size > 0
              ? Array.from(multiSelect)
              : selectedIndex >= 0
                ? [selectedIndex]
                : [];
          if (indices.length > 0) {
            dispatch({ type: "APPLY_OP", op: createDeleteCues(indices) });
            const minIdx = Math.min(...indices);
            setSelectedIndex(
              Math.min(minIdx, docState.cues.length - indices.length - 1),
            );
            setMultiSelect(new Set());
          }
          break;
        }
        case "split": {
          if (selectedIndex >= 0 && selectedIndex < docState.cues.length) {
            const cue = docState.cues[selectedIndex];
            // Use cursor position if available, otherwise midpoint
            const splitAt =
              cursorPosRef.current > 0 && cursorPosRef.current < cue.text.length
                ? cursorPosRef.current
                : Math.floor(cue.text.length / 2);
            dispatch({
              type: "APPLY_OP",
              op: createSplitCue(selectedIndex, splitAt),
            });
          }
          break;
        }
        case "merge": {
          const indices = Array.from(multiSelect).sort((a, b) => a - b);
          if (indices.length >= 2) {
            // Check consecutive
            let consecutive = true;
            for (let i = 1; i < indices.length; i++) {
              if (indices[i] !== indices[i - 1] + 1) {
                consecutive = false;
                break;
              }
            }
            if (consecutive) {
              dispatch({ type: "APPLY_OP", op: createMergeCues(indices) });
              setSelectedIndex(indices[0]);
              setMultiSelect(new Set());
            }
          }
          break;
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      handleSave,
      handleUpload,
      handleDownload,
      selectedIndex,
      multiSelect,
      docState.cues,
    ],
  );

  // Detail pane handlers
  const handleTextChange = useCallback(
    (text: string) => {
      if (selectedIndex >= 0 && selectedIndex < docState.cues.length) {
        dispatch({ type: "APPLY_OP", op: createEditText(selectedIndex, text) });
      }
    },
    [selectedIndex, docState.cues.length],
  );

  const handleTimingChange = useCallback(
    (startMs: number, endMs: number) => {
      if (selectedIndex >= 0 && selectedIndex < docState.cues.length) {
        dispatch({
          type: "APPLY_OP",
          op: createEditTiming(selectedIndex, startMs, endMs),
        });
      }
    },
    [selectedIndex, docState.cues.length],
  );

  const handleAdvance = useCallback(() => {
    setSelectedIndex((prev) =>
      prev < docState.cues.length - 1 ? prev + 1 : prev,
    );
  }, [docState.cues.length]);

  // Search/Replace handlers
  const handleSearchNavigate = useCallback((cueIndex: number) => {
    setSelectedIndex(cueIndex);
  }, []);

  const handleJumpToCue = useCallback((index: number) => {
    setSelectedIndex(index);
  }, []);

  const handleSearchReplace = useCallback(
    (cueIndex: number, oldText: string, newText: string) => {
      const cue = docState.cues[cueIndex];
      if (!cue) return;
      // Find exact first occurrence and splice at that offset
      const pos = cue.text.indexOf(oldText);
      if (pos === -1) return;
      const updatedText =
        cue.text.slice(0, pos) + newText + cue.text.slice(pos + oldText.length);
      dispatch({ type: "APPLY_OP", op: createEditText(cueIndex, updatedText) });
    },
    [docState.cues],
  );

  const handleSearchReplaceAll = useCallback(
    (pattern: string, replacement: string) => {
      // Batch all replacements into a single undoable operation
      const ops: ReturnType<typeof createEditText>[] = [];
      const regex = new RegExp(pattern, "gi");
      for (let i = 0; i < docState.cues.length; i++) {
        const cue = docState.cues[i];
        regex.lastIndex = 0;
        if (regex.test(cue.text)) {
          regex.lastIndex = 0;
          const newText = cue.text.replace(regex, replacement);
          ops.push(createEditText(i, newText));
        }
      }
      if (ops.length > 0) {
        dispatch({ type: "BATCH_OPS", ops });
      }
    },
    [docState.cues],
  );

  // QC fix handler
  const handleQCApplyFixes = useCallback(
    (ops: ReturnType<typeof createEditText>[]) => {
      if (ops.length > 0) {
        dispatch({ type: "BATCH_OPS", ops });
      }
    },
    [],
  );

  const handleQCNavigate = useCallback((cueIndex: number) => {
    setSelectedIndex(cueIndex);
  }, []);

  const handleTimingApplyBatch = useCallback(
    (ops: ReturnType<typeof createEditTiming>[]) => {
      if (ops.length > 0) {
        dispatch({ type: "BATCH_OPS", ops });
      }
    },
    [],
  );

  const handleWaveformTimingChange = useCallback(
    (index: number, startMs: number, endMs: number) => {
      dispatch({
        type: "APPLY_OP",
        op: createEditTiming(index, startMs, endMs),
      });
    },
    [],
  );

  // User explicitly selects a cue (click in table, waveform, etc.)
  const handleUserSelect = useCallback(
    (index: number) => {
      setSelectedIndex(index);
      if (index >= 0 && index < docState.cues.length) {
        setUserSeekMs(docState.cues[index].startMs);
        setSeekCounter((c) => c + 1);
      }
    },
    [docState.cues],
  );

  // Waveform seek: find cue at that time and seek video there
  const handleWaveformSeek = useCallback(
    (ms: number) => {
      const idx = docState.cues.findIndex(
        (c) => ms >= c.startMs && ms <= c.endMs,
      );
      if (idx >= 0) setSelectedIndex(idx);
      setUserSeekMs(ms);
      setSeekCounter((c) => c + 1);
    },
    [docState.cues],
  );

  const handleApplyTranslation = useCallback(
    (translations: Map<number, string>) => {
      // If translating from reference cues, create all cues with reference timing
      if (
        referenceCueList.length > 0 &&
        translations.size > docState.cues.length
      ) {
        const newCues: Cue[] = [];
        translations.forEach((text, idx) => {
          if (idx < referenceCueList.length) {
            newCues.push({
              id: crypto.randomUUID(),
              startMs: referenceCueList[idx].startMs,
              endMs: referenceCueList[idx].endMs,
              text,
            });
          }
        });
        if (newCues.length > 0) {
          // Sort by time to ensure correct order regardless of Map iteration order
          newCues.sort((a, b) => a.startMs - b.startMs);
          dispatch({ type: "LOAD", cues: newCues });
          dispatch({ type: "MARK_DIRTY" });
          showNotification({
            title: "Translation applied",
            message: `Created ${newCues.length} translated cue(s)`,
            color: "green",
          });
        }
      } else {
        // Normal case: update existing editor cues
        const ops = Array.from(translations.entries())
          .filter(([idx]) => idx >= 0 && idx < docState.cues.length)
          .map(([idx, text]) => createEditText(idx, text));
        if (ops.length > 0) {
          dispatch({ type: "BATCH_OPS", ops });
          showNotification({
            title: "Translation applied",
            message: `Updated ${ops.length} cue(s)`,
            color: "green",
          });
        }
      }
      setTranslating(false);
    },
    [docState.cues, referenceCueList],
  );

  // Load reference subtitle from server
  const handleLoadReference = useCallback(
    async (lang: string) => {
      if (!mediaType || !mediaId) return;
      try {
        const result = await api.subtitles.getContent(
          mediaType,
          Number(mediaId),
          lang,
        );
        if (result.data.exists === false || !result.data.content) {
          showNotification({
            title: "No content",
            message: `No subtitle found for ${lang}`,
            color: "yellow",
          });
          return;
        }
        const parsed = getParser(result.data.format as SubtitleFormat).parse(
          result.data.content,
        );
        setReferenceCueList(
          parsed.cues.map((c) => ({
            startMs: c.startMs,
            endMs: c.endMs,
            text: c.text,
          })),
        );
        setReferenceLanguage(lang);
        showNotification({
          message: `Loaded ${parsed.cues.length} reference cues (${lang})`,
          color: "teal",
          autoClose: 2000,
        });
      } catch {
        showNotification({
          title: "Error",
          message: "Failed to load reference subtitle",
          color: "red",
        });
      }
    },
    [mediaType, mediaId],
  );

  // Import reference from file
  const handleImportReference = useCallback(() => {
    refFileInputRef.current?.click();
  }, []);

  const handleRefFileSelected = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const text = reader.result as string;
        try {
          const fmt = detectFormat(file.name) ?? "srt";
          const parsed = getParser(fmt).parse(text);
          setReferenceCueList(
            parsed.cues.map((c) => ({
              startMs: c.startMs,
              endMs: c.endMs,
              text: c.text,
            })),
          );
          setReferenceLanguage(file.name);
        } catch {
          showNotification({
            title: "Parse error",
            message: "Could not parse reference file",
            color: "red",
          });
        }
      };
      reader.readAsText(file);
      e.target.value = "";
    },
    [],
  );

  // Use reference text: create new cue at reference timing, or update selected cue if it overlaps
  const handleUseReference = useCallback(() => {
    if (referenceCueList.length === 0) return;

    const pos = playbackTimeMs;
    const cue =
      selectedIndex >= 0 && selectedIndex < docState.cues.length
        ? docState.cues[selectedIndex]
        : null;
    let matches = referenceCueList.filter(
      (r) => r.startMs <= pos && r.endMs >= pos,
    );
    if (matches.length === 0 && cue) {
      matches = referenceCueList.filter(
        (r) => r.startMs < cue.endMs && r.endMs > cue.startMs,
      );
    }
    const text = matches.map((r) => r.text).join("\n");
    if (!text || matches.length === 0) return;

    const refStartMs = Math.min(...matches.map((r) => r.startMs));
    const refEndMs = Math.max(...matches.map((r) => r.endMs));

    // If selected cue overlaps the reference, update it in place
    if (cue && cue.startMs < refEndMs && cue.endMs > refStartMs) {
      dispatch({ type: "APPLY_OP", op: createEditText(selectedIndex, text) });
    } else {
      // Create new cue at reference timing
      const newCue: Cue = {
        id: crypto.randomUUID(),
        startMs: refStartMs,
        endMs: refEndMs,
        text,
      };
      let insertIdx = docState.cues.findIndex((c) => c.startMs > refStartMs);
      if (insertIdx === -1) insertIdx = docState.cues.length;
      dispatch({ type: "APPLY_OP", op: createAddCue(insertIdx - 1, newCue) });
      setSelectedIndex(insertIdx);
    }
  }, [selectedIndex, docState.cues, referenceCueList, playbackTimeMs]);

  // Translate single line via API
  const handleTranslateLine = useCallback(async () => {
    const cue =
      selectedIndex >= 0 && selectedIndex < docState.cues.length
        ? docState.cues[selectedIndex]
        : null;

    // Find reference at playback position first, then cue overlap
    const pos = playbackTimeMs;
    let refMatches = referenceCueList.filter(
      (r) => r.startMs <= pos && r.endMs >= pos,
    );
    if (refMatches.length === 0 && cue) {
      refMatches = referenceCueList.filter(
        (r) => r.startMs < cue.endMs && r.endMs > cue.startMs,
      );
    }

    const sourceText =
      refMatches.length > 0
        ? refMatches.map((r) => r.text).join("\n")
        : cue?.text || "";
    if (!sourceText.trim()) return;

    // Create cue at reference timing if no selected cue overlaps the reference
    let targetIdx = selectedIndex;
    if (refMatches.length > 0) {
      const refStartMs = Math.min(...refMatches.map((r) => r.startMs));
      const refEndMs = Math.max(...refMatches.map((r) => r.endMs));
      const overlaps = cue && cue.startMs < refEndMs && cue.endMs > refStartMs;
      if (!overlaps) {
        const newCue: Cue = {
          id: crypto.randomUUID(),
          startMs: refStartMs,
          endMs: refEndMs,
          text: "",
        };
        let insertIdx = docState.cues.findIndex((c) => c.startMs > refStartMs);
        if (insertIdx === -1) insertIdx = docState.cues.length;
        dispatch({ type: "APPLY_OP", op: createAddCue(insertIdx - 1, newCue) });
        setSelectedIndex(insertIdx);
        targetIdx = insertIdx;
      }
    }

    setTranslatingLineIdx(targetIdx);
    try {
      const sourceLangName = referenceLanguage || "";
      const result = await client.axios.post("/translator/jobs", {
        lines: [{ position: 0, line: sourceText }],
        sourceLanguage: sourceLangName,
        targetLanguage: language || "",
        title: data?.mediaTitle || "",
        mediaType: mediaType || "",
      });
      const jobResult = result.data;
      if (!jobResult.jobId) {
        setTranslatingLineIdx(null);
        return;
      }
      const poll = async () => {
        for (let i = 0; i < 120; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          const resp = await client.axios.get(
            `/translator/jobs/${jobResult.jobId}`,
          );
          const job = resp.data;
          if (job.status === "completed" || job.status === "partial") {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const lines = (job.result as any)?.lines;
            if (lines?.[0]?.line) {
              dispatch({
                type: "APPLY_OP",
                op: createEditText(targetIdx, lines[0].line),
              });
            }
            break;
          } else if (job.status === "failed" || job.status === "cancelled") {
            break;
          }
        }
        setTranslatingLineIdx(null);
      };
      poll();
    } catch {
      setTranslatingLineIdx(null);
    }
  }, [
    selectedIndex,
    docState.cues,
    referenceCueList,
    playbackTimeMs,
    referenceLanguage,
    language,
    data?.mediaTitle,
    mediaType,
  ]);

  // Clear auto-focus flag after it fires
  useEffect(() => {
    if (shouldFocusTextarea) {
      // Reset on next tick so the DetailPane has time to pick it up
      const id = requestAnimationFrame(() => setShouldFocusTextarea(false));
      return () => cancelAnimationFrame(id);
    }
  }, [shouldFocusTextarea]);

  // Create cue from ghost gap
  const handleCreateFromGap = useCallback(
    (startMs: number, endMs: number, afterIndex: number) => {
      const newCue: Cue = {
        id: crypto.randomUUID(),
        startMs,
        endMs,
        text: "",
      };
      dispatch({ type: "APPLY_OP", op: createAddCue(afterIndex, newCue) });
      setSelectedIndex(afterIndex + 1);
    },
    [],
  );

  // Helper: nudge timing on selected cue
  const nudgeTiming = useCallback(
    (mode: "start" | "end" | "both", direction: number, large: boolean) => {
      if (selectedIndex < 0 || selectedIndex >= docState.cues.length) return;
      const cue = docState.cues[selectedIndex];
      const step = (large ? 1000 : 100) * direction;
      let newStart = cue.startMs;
      let newEnd = cue.endMs;
      if (mode === "start" || mode === "both")
        newStart = Math.max(0, newStart + step);
      if (mode === "end" || mode === "both")
        newEnd = Math.max(0, newEnd + step);
      if (newEnd < newStart) newEnd = newStart; // prevent negative duration
      dispatch({
        type: "APPLY_OP",
        op: createEditTiming(selectedIndex, newStart, newEnd),
      });
    },
    [selectedIndex, docState.cues],
  );

  // Keyboard shortcuts (Subtitle Edit conventions)
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const target = e.target as HTMLElement;
      const isTextInput =
        target.tagName === "TEXTAREA" ||
        target.tagName === "INPUT" ||
        target.isContentEditable;

      const ctrl = e.ctrlKey || e.metaKey;

      // -- Global shortcuts (work even in text inputs) --

      // Ctrl+S: save
      if (e.key === "s" && ctrl) {
        e.preventDefault();
        handleSave();
        return;
      }

      // Ctrl+F: search/replace
      if (e.key === "f" && ctrl) {
        e.preventDefault();
        setSearchOpen((v) => !v);
        return;
      }

      // Alt+T: translate panel
      if (e.key === "t" && e.altKey && !ctrl && !e.shiftKey) {
        e.preventDefault();
        setTranslateOpen((v) => !v);
        return;
      }

      // Ctrl+Shift+R: reference panel
      if (e.key === "R" && ctrl && e.shiftKey) {
        e.preventDefault();
        setReferenceOpen((v) => !v);
        return;
      }

      // Ctrl+Shift+Space: toggle video play/pause
      if (e.key === " " && ctrl && e.shiftKey) {
        e.preventDefault();
        videoPreviewRef.current?.togglePlay();
        return;
      }

      // Alt+Left/Right: seek -5/+5 seconds
      if (e.key === "ArrowLeft" && e.altKey && !ctrl && !e.shiftKey) {
        e.preventDefault();
        videoPreviewRef.current?.seekRelative(-5000);
        return;
      }
      if (e.key === "ArrowRight" && e.altKey && !ctrl && !e.shiftKey) {
        e.preventDefault();
        videoPreviewRef.current?.seekRelative(5000);
        return;
      }

      // -- Timing nudge shortcuts (work even in text inputs, Subtitle Edit style) --

      // Ctrl+Shift+Left/Right: nudge START time earlier/later
      if (e.key === "ArrowLeft" && ctrl && e.shiftKey && !e.altKey) {
        e.preventDefault();
        nudgeTiming("start", -1, false);
        return;
      }
      if (e.key === "ArrowRight" && ctrl && e.shiftKey && !e.altKey) {
        e.preventDefault();
        nudgeTiming("start", 1, false);
        return;
      }

      // Alt+Shift+Left/Right: nudge END time earlier/later
      if (e.key === "ArrowLeft" && e.altKey && e.shiftKey && !ctrl) {
        e.preventDefault();
        nudgeTiming("end", -1, false);
        return;
      }
      if (e.key === "ArrowRight" && e.altKey && e.shiftKey && !ctrl) {
        e.preventDefault();
        nudgeTiming("end", 1, false);
        return;
      }

      // Ctrl+Alt+Left/Right: move entire cue (both start and end)
      if (e.key === "ArrowLeft" && ctrl && e.altKey) {
        e.preventDefault();
        nudgeTiming("both", -1, false);
        return;
      }
      if (e.key === "ArrowRight" && ctrl && e.altKey) {
        e.preventDefault();
        nudgeTiming("both", 1, false);
        return;
      }

      // Ctrl+Shift+Up/Down: adjust duration (expand/shrink end time)
      if (e.key === "ArrowUp" && ctrl && e.shiftKey) {
        e.preventDefault();
        nudgeTiming("end", 1, false);
        return;
      }
      if (e.key === "ArrowDown" && ctrl && e.shiftKey) {
        e.preventDefault();
        nudgeTiming("end", -1, false);
        return;
      }

      // Ctrl+G: jump to cue
      if (e.key === "g" && ctrl && !e.shiftKey) {
        e.preventDefault();
        setJumpOpen((v) => !v);
        return;
      }

      // Ctrl+B: toggle bookmark on selected cue
      if (e.key === "b" && ctrl && !e.shiftKey) {
        e.preventDefault();
        if (selectedIndex >= 0 && selectedIndex < docState.cues.length) {
          toggleBookmark(docState.cues[selectedIndex].id);
        }
        return;
      }

      // Ctrl+Shift+B: jump to next bookmarked cue
      if (e.key === "b" && ctrl && e.shiftKey) {
        e.preventDefault();
        if (bookmarkedIds.size > 0) {
          const startFrom = selectedIndex + 1;
          for (let i = 0; i < docState.cues.length; i++) {
            const idx = (startFrom + i) % docState.cues.length;
            if (bookmarkedIds.has(docState.cues[idx].id)) {
              setSelectedIndex(idx);
              break;
            }
          }
        }
        return;
      }

      // -- Cue operation shortcuts (work everywhere including text inputs) --
      // Blur active element first to commit any pending text edits
      const commitPendingEdit = () => {
        if (document.activeElement instanceof HTMLElement && isTextInput) {
          document.activeElement.blur();
        }
      };

      // Ctrl+Shift+Enter: add new cue
      if (e.key === "Enter" && ctrl && e.shiftKey) {
        e.preventDefault();
        commitPendingEdit();
        // Use setTimeout to let the blur commit propagate before adding
        setTimeout(() => handleToolbarAction("addCue"), 0);
        return;
      }

      // Ctrl+Shift+M: merge cues
      if (e.key === "m" && ctrl && e.shiftKey) {
        e.preventDefault();
        commitPendingEdit();
        setTimeout(() => handleToolbarAction("merge"), 0);
        return;
      }

      // Ctrl+Shift+S: split cue
      if (e.key === "s" && ctrl && e.shiftKey) {
        e.preventDefault();
        commitPendingEdit();
        setTimeout(() => handleToolbarAction("split"), 0);
        return;
      }

      // -- Non-text-input shortcuts --
      if (isTextInput) return;

      // ?: shortcut cheat sheet
      if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
        e.preventDefault();
        setShortcutsOpen((v) => !v);
        return;
      }

      // Ctrl+Z: undo
      if (e.key === "z" && ctrl && !e.shiftKey) {
        e.preventDefault();
        dispatch({ type: "UNDO" });
        return;
      }

      // Ctrl+Y / Ctrl+Shift+Z: redo
      if ((e.key === "y" && ctrl) || (e.key === "z" && ctrl && e.shiftKey)) {
        e.preventDefault();
        dispatch({ type: "REDO" });
        return;
      }

      // Ctrl+Delete: delete cue
      if (e.key === "Delete" && ctrl) {
        e.preventDefault();
        handleToolbarAction("delete");
        return;
      }

      // Delete (plain): also delete cue when table focused
      if (e.key === "Delete") {
        e.preventDefault();
        handleToolbarAction("delete");
        return;
      }

      // Alt+Down/Up: navigate cues (works from anywhere outside text input)
      if (e.key === "ArrowDown" && e.altKey) {
        e.preventDefault();
        setSelectedIndex((prev) =>
          Math.min(prev + 1, docState.cues.length - 1),
        );
        return;
      }
      if (e.key === "ArrowUp" && e.altKey) {
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
        return;
      }

      // [ / ]: move entire cue backward/forward (legacy, still useful)
      if (e.key === "[") {
        e.preventDefault();
        nudgeTiming("both", -1, e.shiftKey);
        return;
      }
      if (e.key === "]") {
        e.preventDefault();
        nudgeTiming("both", 1, e.shiftKey);
        return;
      }
    },
    [
      handleSave,
      handleToolbarAction,
      nudgeTiming,
      selectedIndex,
      docState.cues,
      toggleBookmark,
      bookmarkedIds,
    ],
  );

  // Find reference text aligned by time to the current playback position
  const selectedRefText = useMemo(() => {
    if (referenceCueList.length === 0) return undefined;
    // Use playback position to find matching reference cues
    const pos = playbackTimeMs;
    const matches = referenceCueList.filter(
      (r) => r.startMs <= pos && r.endMs >= pos,
    );
    // Fall back to selected cue overlap if nothing at exact position
    if (matches.length === 0) {
      const cue =
        selectedIndex >= 0 && selectedIndex < docState.cues.length
          ? docState.cues[selectedIndex]
          : null;
      if (!cue) return undefined;
      const cueMatches = referenceCueList.filter(
        (r) => r.startMs < cue.endMs && r.endMs > cue.startMs,
      );
      if (cueMatches.length === 0) return undefined;
      return cueMatches.map((r) => r.text).join("\n");
    }
    return matches.map((r) => r.text).join("\n");
  }, [playbackTimeMs, selectedIndex, docState.cues, referenceCueList]);

  // Start with empty cues for new subtitle if not yet loaded
  useEffect(() => {
    if (isNewSubtitle && !loadedRef.current) {
      dispatch({ type: "LOAD", cues: [] });
      loadedRef.current = true;
    }
  }, [isNewSubtitle]);

  // Loading state
  if (isLoading) {
    return (
      <Center style={{ height: "100%" }}>
        <Loader />
      </Center>
    );
  }

  // Real error (not "create new" mode)
  if (error && !isNewSubtitle) {
    return (
      <Alert color="red" title="Error" m="md">
        {error.message || "Failed to load subtitle content"}
      </Alert>
    );
  }

  // Parse failure (only when we have data but parsing failed)
  if (data && !parseResult) {
    return (
      <Alert color="red" title="Parse Error" m="md">
        Failed to parse subtitle file
      </Alert>
    );
  }

  // Normal waiting state (shouldn't happen after loading/error checks)
  if (!data && !isNewSubtitle) {
    return null;
  }

  // Breadcrumb links
  const isSeries = mediaType === "episode" || mediaType === "series";
  const listPath = isSeries ? "/series" : "/movies";
  const listLabel = isSeries ? "Series" : "Movies";
  const detailPath = data?.mediaId
    ? isSeries
      ? `/series/${data.mediaId}`
      : `/movies/${data.mediaId}`
    : undefined;

  const selectedCue =
    selectedIndex >= 0 && selectedIndex < docState.cues.length
      ? docState.cues[selectedIndex]
      : null;
  const activeSubtitleText = getActiveCueText(docState.cues, playbackTimeMs);

  return (
    <div
      onKeyDown={handleKeyDown}
      tabIndex={-1}
      style={{
        height: "calc(100dvh - var(--app-shell-header-offset, 76px) - 1px)",
        maxHeight: "calc(100dvh - var(--app-shell-header-offset, 76px) - 1px)",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        outline: "none",
      }}
    >
      {/* Breadcrumb + badges */}
      <Group justify="space-between" px="md" pt="xs" pb={4}>
        <Breadcrumbs>
          <Anchor component={Link} to={listPath}>
            {listLabel}
          </Anchor>
          {data?.mediaTitle && detailPath && (
            <Anchor component={Link} to={detailPath}>
              {data.mediaTitle}
            </Anchor>
          )}
          {data?.episodeTitle && <Text>{data.episodeTitle}</Text>}
          <Menu shadow="md" width={260} position="bottom-start" withinPortal>
            <Menu.Target>
              <Anchor
                component="button"
                type="button"
                style={{
                  cursor: "pointer",
                  fontSize: "inherit",
                  fontFamily: "inherit",
                }}
              >
                {(() => {
                  const sub = availableSubtitles.find(
                    (s) => s.language === language,
                  );
                  if (sub) {
                    const name =
                      (serverLanguages ?? []).find(
                        (l) => l.code2 === sub.language.split(":")[0],
                      )?.name ?? sub.language;
                    const mod = sub.language.includes(":")
                      ? ` (${sub.language.split(":")[1].toUpperCase()})`
                      : "";
                    return `${name}${mod} [${sub.format}]`;
                  }
                  const name =
                    (serverLanguages ?? []).find(
                      (l) => l.code2 === language?.split(":")[0],
                    )?.name ?? language;
                  return isNewSubtitle ? `${name} (new)` : name;
                })()}
                {" \u25BE"}
              </Anchor>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Existing subtitles</Menu.Label>
              {availableSubtitles.map((s) => {
                const langName =
                  (serverLanguages ?? []).find(
                    (l) => l.code2 === s.language.split(":")[0],
                  )?.name ?? s.language;
                const modifier = s.language.includes(":")
                  ? ` (${s.language.split(":")[1].toUpperCase()})`
                  : "";
                return (
                  <Menu.Item
                    key={s.language}
                    onClick={() => switchToLanguage(s.language)}
                    style={
                      s.language === language
                        ? { background: "rgba(230,138,0,0.12)" }
                        : undefined
                    }
                  >
                    {langName}
                    {modifier} [{s.format}]
                  </Menu.Item>
                );
              })}
              {availableSubtitles.length === 0 && (
                <Menu.Item disabled>No subtitles found</Menu.Item>
              )}
              <Menu.Divider />
              <Menu.Label>Create new</Menu.Label>
              {languageOptions
                .filter(
                  (opt) =>
                    !availableSubtitles.some((s) => s.language === opt.value) &&
                    opt.value !== language,
                )
                .slice(0, 20)
                .map((opt) => (
                  <Menu.Item
                    key={opt.value}
                    onClick={() => switchToLanguage(opt.value)}
                  >
                    {opt.label}
                  </Menu.Item>
                ))}
            </Menu.Dropdown>
          </Menu>
        </Breadcrumbs>
        <Group gap="xs">
          {isNewSubtitle && (
            <Badge variant="light" color="yellow" size="sm">
              NEW
            </Badge>
          )}
        </Group>
      </Group>

      {/* Toolbar */}
      <EditorToolbar
        onAction={handleToolbarAction}
        dirty={docState.dirty}
        canUndo={docState.undoStack.length > 0}
        canRedo={docState.redoStack.length > 0}
        format={format}
        language={data?.language ?? language ?? ""}
        bookmarkFilterActive={bookmarkFilterActive}
        bookmarkCount={bookmarkedIds.size}
        onToggleBookmarkFilter={() => setBookmarkFilterActive((v) => !v)}
        onShowShortcuts={() => setShortcutsOpen(true)}
        translating={translating}
        referenceActive={referenceCueList.length > 0}
      />

      {/* Auto-save recovery banner */}
      {recoveryAvailable && (
        <Group
          gap="sm"
          px="md"
          py={6}
          style={{
            background: "rgba(251, 191, 36, 0.1)",
            borderBottom: "1px solid rgba(251, 191, 36, 0.2)",
            flexShrink: 0,
          }}
        >
          <Text size="sm" c="#fbbf24">
            Unsaved draft found ({recoveryAvailable.cueCount} cues,{" "}
            {new Date(recoveryAvailable.timestamp).toLocaleTimeString()})
          </Text>
          <Button
            size="xs"
            variant="light"
            color="yellow"
            onClick={handleRecoverDraft}
          >
            Recover
          </Button>
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            onClick={handleDismissRecovery}
          >
            Dismiss
          </Button>
        </Group>
      )}

      {/* Main content: table left, video+detail right */}
      <div
        style={{
          flex: "1 1 auto",
          minHeight: 0,
          overflow: "hidden",
          display: "flex",
          gap: 0,
          padding: "0 4px 0 var(--mantine-spacing-md)",
        }}
      >
        {/* Left: Cue table with overlays */}
        <div
          style={{
            flex: "1 1 60%",
            minWidth: 0,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            position: "relative",
          }}
        >
          {docState.cues.length === 0 ? (
            <div
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                gap: 20,
                border: "1px dashed rgba(255,255,255,0.15)",
                borderRadius: "var(--mantine-radius-md)",
                padding: 40,
              }}
            >
              <Text size="xl" fw={600} c="dimmed">
                No cues yet
              </Text>
              <Text size="sm" c="dimmed" ta="center" maw={400}>
                Start by adding a cue manually, uploading an existing subtitle
                file, or pasting subtitle content.
              </Text>
              <Group gap="md">
                <Button
                  variant="light"
                  leftSection={<span style={{ fontSize: 16 }}>+</span>}
                  onClick={() => handleToolbarAction("addCue")}
                >
                  Add First Cue
                </Button>
                <Button
                  variant="light"
                  color="teal"
                  onClick={() => handleToolbarAction("upload")}
                >
                  Upload File
                </Button>
              </Group>
            </div>
          ) : (
            <>
              <EditableCueTable
                cues={docState.cues}
                selectedIndex={selectedIndex}
                multiSelect={multiSelect}
                warnings={warnings}
                ghostCues={ghostCues}
                modifiedCueIds={docState.modifiedCueIds}
                bookmarkedIds={bookmarkedIds}
                visibleCueIds={
                  bookmarkFilterActive && bookmarkedIds.size > 0
                    ? bookmarkedIds
                    : undefined
                }
                onSelect={handleUserSelect}
                onMultiSelect={setMultiSelect}
                onActivate={handleUserSelect}
                onCreateFromGap={handleCreateFromGap}
                onToggleBookmark={toggleBookmark}
              />
            </>
          )}
          <SearchReplace
            open={searchOpen}
            cues={docState.cues}
            onNavigate={handleSearchNavigate}
            onReplace={handleSearchReplace}
            onReplaceAll={handleSearchReplaceAll}
            onClose={() => setSearchOpen(false)}
          />
          <JumpToCue
            open={jumpOpen}
            cueCount={docState.cues.length}
            onJump={handleJumpToCue}
            onClose={() => setJumpOpen(false)}
          />
          <QCPanel
            open={qcOpen}
            cues={docState.cues}
            preset={qcPreset}
            combined={isCombined}
            onPresetChange={setQcPreset}
            onApplyFixes={handleQCApplyFixes}
            onNavigate={handleQCNavigate}
            onClose={() => setQcOpen(false)}
          />
          <TimingToolsPanel
            open={timingOpen}
            cues={docState.cues}
            selectedIndices={multiSelect}
            mediaType={mediaType}
            mediaId={mediaId ? Number(mediaId) : undefined}
            language={language}
            onApplyBatch={handleTimingApplyBatch}
            onGetContent={() => {
              const pr = buildParseResult();
              const serialized = getSerializer(format).serialize(pr);
              return { content: serialized, format, encoding };
            }}
            onApplySyncedContent={(syncedContent: string) => {
              try {
                const parsed = getParser(format).parse(syncedContent);
                if (parsed.cues.length === 0) {
                  showNotification({
                    title: "Sync",
                    message: "Synced content has no cues",
                    color: "yellow",
                  });
                  return;
                }
                dispatch({ type: "LOAD", cues: parsed.cues });
                showNotification({
                  message: `Loaded ${parsed.cues.length} synced cues into editor`,
                  color: "green",
                  autoClose: 3000,
                });
              } catch (err) {
                showNotification({
                  title: "Error",
                  message: `Failed to parse synced content: ${(err as Error).message}`,
                  color: "red",
                });
              }
            }}
            onClose={() => setTimingOpen(false)}
          />
          <TranslatePanel
            open={translateOpen}
            cues={docState.cues}
            referenceCues={referenceCueList}
            availableSubtitles={availableSubtitles}
            mediaType={mediaType}
            mediaId={mediaId ? Number(mediaId) : undefined}
            currentLanguage={language ?? ""}
            mediaTitle={data?.mediaTitle}
            onApplyTranslation={handleApplyTranslation}
            onSetReference={(map) => {
              const arr = Array.from(map.entries())
                .filter(([idx]) => idx >= 0 && idx < docState.cues.length)
                .map(([idx, text]) => ({
                  startMs: docState.cues[idx].startMs,
                  endMs: docState.cues[idx].endMs,
                  text,
                }));
              setReferenceCueList(arr);
            }}
            onTranslatingChange={setTranslating}
            onLoadReference={handleLoadReference}
            onImportReference={handleImportReference}
            referenceLanguage={referenceLanguage}
            onClose={() => setTranslateOpen(false)}
          />
          {/* Reference panel */}
          {referenceOpen && (
            <div
              style={{
                position: "absolute",
                top: 8,
                right: 8,
                zIndex: 10,
                minWidth: 320,
                background: "var(--bz-surface-raised)",
                border: "1px solid var(--bz-border-card)",
                borderRadius: "var(--bz-radius-sm)",
                padding: "10px 14px",
                boxShadow: "var(--bz-shadow-float)",
                display: "flex",
                flexDirection: "column",
                gap: 8,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: "var(--bz-text-primary)",
                  }}
                >
                  Reference Subtitle
                </span>
                <button
                  type="button"
                  onClick={() => setReferenceOpen(false)}
                  style={{
                    background: "none",
                    border: "none",
                    color: "var(--bz-text-tertiary)",
                    cursor: "pointer",
                    fontSize: 14,
                  }}
                >
                  x
                </button>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <select
                  style={{
                    flex: 1,
                    background: "var(--bz-surface-base)",
                    border: "1px solid var(--bz-border-interactive)",
                    borderRadius: "var(--bz-radius-xs)",
                    color: "var(--bz-text-primary)",
                    fontSize: 12,
                    padding: "4px 6px",
                  }}
                  value={referenceLanguage ?? ""}
                  onChange={(e) => {
                    if (e.target.value) handleLoadReference(e.target.value);
                  }}
                >
                  <option value="">Select language...</option>
                  {availableSubtitles.length > 0
                    ? availableSubtitles
                        .filter((s) => s.language !== language)
                        .map((s) => {
                          const langName =
                            (serverLanguages ?? []).find(
                              (l) => l.code2 === s.language.split(":")[0],
                            )?.name ?? s.language;
                          const modifier = s.language.includes(":")
                            ? ` (${s.language.split(":")[1].toUpperCase()})`
                            : "";
                          return (
                            <option key={s.language} value={s.language}>
                              {langName}
                              {modifier} [{s.format}]
                            </option>
                          );
                        })
                    : (languageOptions ?? []).map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                </select>
                <button
                  type="button"
                  onClick={handleImportReference}
                  style={{
                    background: "var(--bz-surface-base)",
                    border: "1px solid var(--bz-border-interactive)",
                    borderRadius: "var(--bz-radius-xs)",
                    color: "var(--bz-text-secondary)",
                    fontSize: 11,
                    padding: "4px 8px",
                    cursor: "pointer",
                  }}
                >
                  Import file
                </button>
              </div>
              {referenceCueList.length > 0 && (
                <div style={{ fontSize: 11, color: "var(--bz-text-tertiary)" }}>
                  {referenceCueList.length} cues loaded
                  {referenceLanguage && <span> ({referenceLanguage})</span>}
                  <button
                    type="button"
                    onClick={() => {
                      setReferenceCueList([]);
                      setReferenceLanguage(undefined);
                    }}
                    style={{
                      background: "none",
                      border: "none",
                      color: "var(--bz-stat-failed)",
                      cursor: "pointer",
                      fontSize: 10,
                      marginLeft: 8,
                    }}
                  >
                    Clear
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Right: Video preview + Detail pane */}
        <div
          style={{
            flex: "0 0 38%",
            maxWidth: 520,
            minWidth: 340,
            display: "flex",
            flexDirection: "column",
            gap: 0,
            overflow: "hidden",
            borderLeft: "1px solid var(--bz-border-card)",
          }}
        >
          <VideoPreview
            ref={videoPreviewRef}
            mediaType={mediaType}
            mediaId={mediaId ? Number(mediaId) : undefined}
            currentTimeMs={userSeekMs ?? 0}
            seekId={seekCounter}
            currentSubtitleText={activeSubtitleText}
            onTimeUpdate={(ms) => {
              setPlaybackTimeMs(ms);
              const idx = findActiveCueIndex(docState.cues, ms);
              if (idx >= 0 && idx !== selectedIndex) {
                setSelectedIndex(idx);
              }
            }}
            onPlayStateChange={setVideoPlaying}
            onAudioTracksLoaded={setAudioTracks}
            audioTrack={selectedAudioTrack}
          />
          {/* Audio track selector */}
          {audioTracks.length > 1 && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "3px 8px",
                background: "var(--bz-surface-base)",
                borderBottom: "1px solid var(--bz-border-card)",
                fontSize: 11,
              }}
            >
              <span
                style={{
                  color: "var(--bz-text-tertiary)",
                  whiteSpace: "nowrap",
                }}
              >
                Audio:
              </span>
              <select
                value={selectedAudioTrack}
                onChange={(e) => setSelectedAudioTrack(Number(e.target.value))}
                style={{
                  flex: 1,
                  background: "var(--bz-surface-raised)",
                  border: "1px solid var(--bz-border-interactive)",
                  borderRadius: "var(--bz-radius-xs)",
                  color: "var(--bz-text-primary)",
                  fontSize: 11,
                  padding: "2px 4px",
                  cursor: "pointer",
                }}
              >
                {audioTracks.map((t) => (
                  <option key={t.index} value={t.index}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
          )}
          <DetailPane
            ref={detailPaneRef}
            cue={selectedCue}
            cueIndex={selectedIndex}
            onTextChange={handleTextChange}
            onTimingChange={handleTimingChange}
            onAdvance={handleAdvance}
            cursorPosRef={cursorPosRef}
            autoFocus={shouldFocusTextarea}
            referenceText={
              referenceCueList.length > 0 ? (selectedRefText ?? "") : undefined
            }
            onUseReference={selectedRefText ? handleUseReference : undefined}
            onTranslateLine={handleTranslateLine}
            translatingLine={translatingLineIdx === selectedIndex}
            currentTimeMs={playbackTimeMs}
          />
        </div>
      </div>

      {/* Waveform timeline */}
      <WaveformTimeline
        mediaType={mediaType}
        mediaId={mediaId ? Number(mediaId) : undefined}
        cues={docState.cues}
        selectedIndex={selectedIndex}
        currentTimeMs={playbackTimeMs}
        onSelect={handleUserSelect}
        onTimingChange={handleWaveformTimingChange}
        onSeek={handleWaveformSeek}
        audioTrack={selectedAudioTrack}
      />

      {/* Status bar */}
      <StatusBar
        cueIndex={selectedIndex}
        cueCount={docState.cues.length}
        format={format}
        encoding={encoding}
        dirty={docState.dirty}
        selectedCue={selectedCue}
        multiSelectCount={multiSelect.size}
        bookmarkCount={bookmarkedIds.size}
        totalDurationMs={totalDurationMs}
      />

      <ShortcutSheet
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />

      {/* Hidden file input for upload */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".srt,.ass,.ssa,.vtt,.sub,.smi,.txt,.mpl"
        style={{ display: "none" }}
        onChange={handleFileSelected}
      />
      {/* Hidden file input for reference import */}
      <input
        ref={refFileInputRef}
        type="file"
        accept=".srt,.ass,.ssa,.vtt,.sub,.smi,.txt,.mpl"
        style={{ display: "none" }}
        onChange={handleRefFileSelected}
      />

      {/* Switch subtitle language confirmation */}
      <Modal
        opened={!!pendingLangSwitch}
        onClose={() => setPendingLangSwitch(null)}
        withCloseButton={false}
        centered
        size="xs"
        padding="md"
        overlayProps={{ backgroundOpacity: 0.4 }}
      >
        <Text size="sm" mb="md">
          Save changes before switching subtitle?
        </Text>
        <Group justify="flex-end" gap="xs">
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            onClick={() => setPendingLangSwitch(null)}
          >
            Cancel
          </Button>
          <Button
            size="xs"
            variant="light"
            color="red"
            onClick={handleSwitchDrop}
          >
            Discard
          </Button>
          <Button size="xs" color="green" onClick={handleSwitchSave}>
            Save
          </Button>
        </Group>
      </Modal>

      {/* Unsaved changes confirmation modal */}
      <Modal
        opened={blocker.state === "blocked"}
        onClose={() => blocker.reset?.()}
        title="Unsaved changes"
        centered
        size="sm"
      >
        <Stack gap="md">
          <Text size="sm">
            You have unsaved changes. Are you sure you want to leave?
          </Text>
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={() => blocker.reset?.()}>
              Stay
            </Button>
            <Button color="red" onClick={() => blocker.proceed?.()}>
              Leave
            </Button>
          </Group>
        </Stack>
      </Modal>

      {/* Save As modal */}
      <Modal
        opened={saveAsOpen}
        onClose={() => setSaveAsOpen(false)}
        title="Save As New Subtitle"
        centered
        size="sm"
      >
        <Stack gap="md">
          <Select
            label="Language"
            placeholder="Select language"
            data={languageOptions}
            value={saveAsLang}
            onChange={setSaveAsLang}
            searchable
            autoFocus
          />
          <Text size="xs" c="dimmed">
            Creates a new subtitle file for this media in the selected language.
            Format: {format.toUpperCase()}
          </Text>
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={() => setSaveAsOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!saveAsLang}
              loading={createMutation.isPending}
              onClick={() => saveAsLang && handleSaveAs(saveAsLang)}
            >
              Save
            </Button>
          </Group>
        </Stack>
      </Modal>

      {/* Overwrite confirmation modal */}
      <Modal
        opened={overwriteConfirm !== null}
        onClose={() => setOverwriteConfirm(null)}
        title="Subtitle Already Exists"
        centered
        size="sm"
      >
        <Stack gap="md">
          <Text size="sm">
            A subtitle file for <strong>{overwriteConfirm?.lang}</strong>{" "}
            already exists. Do you want to overwrite it?
          </Text>
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={() => setOverwriteConfirm(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              loading={saveMutation.isPending}
              onClick={handleOverwrite}
            >
              Overwrite
            </Button>
          </Group>
        </Stack>
      </Modal>
    </div>
  );
}
