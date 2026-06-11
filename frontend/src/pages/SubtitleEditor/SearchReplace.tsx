import {
  type CSSProperties,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

interface SearchReplaceProps {
  open: boolean;
  cues: Array<{ id: string; text: string }>;
  onNavigate: (cueIndex: number) => void;
  onReplace: (
    cueIndex: number,
    startChar: number,
    endChar: number,
    newText: string,
  ) => void;
  onReplaceAll: (
    pattern: string,
    newText: string,
    caseSensitive: boolean,
    isRegex: boolean,
  ) => void;
  onClose: () => void;
}

interface SearchMatch {
  cueIndex: number;
  startChar: number;
  endChar: number;
}

const styles = {
  container: {
    position: "absolute",
    top: 8,
    right: 8,
    zIndex: 10,
    minWidth: 380,
    background: "var(--bz-surface-raised)",
    border: "1px solid var(--bz-border-card)",
    borderRadius: "var(--bz-radius-sm)",
    padding: "8px 12px",
    boxShadow: "var(--bz-shadow-float)",
    display: "flex",
    flexDirection: "column",
    gap: 6,
  } satisfies CSSProperties,

  row: {
    display: "flex",
    alignItems: "center",
    gap: 4,
  } satisfies CSSProperties,

  input: {
    flex: 1,
    fontFamily: "monospace",
    fontSize: 13,
    background: "var(--bz-surface-base)",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-primary)",
    borderRadius: 4,
    padding: "6px 8px",
    outline: "none",
  } satisfies CSSProperties,

  btn: {
    background: "transparent",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-secondary)",
    borderRadius: 4,
    padding: "4px 8px",
    cursor: "pointer",
    fontSize: 12,
    lineHeight: 1,
    flexShrink: 0,
  } satisfies CSSProperties,

  btnActive: {
    background: "var(--bz-surface-card)",
    color: "var(--bz-text-primary)",
  } satisfies CSSProperties,

  counter: {
    fontSize: 11,
    color: "var(--bz-text-tertiary)",
    whiteSpace: "nowrap",
    padding: "0 4px",
    flexShrink: 0,
    minWidth: 60,
    textAlign: "center",
  } satisfies CSSProperties,

  replaceBtn: {
    background: "var(--bz-surface-card)",
    border: "1px solid var(--bz-border-interactive)",
    color: "var(--bz-text-primary)",
    borderRadius: 4,
    padding: "4px 10px",
    cursor: "pointer",
    fontSize: 12,
    flexShrink: 0,
  } satisfies CSSProperties,
};

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function SearchReplace({
  open,
  cues,
  onNavigate,
  onReplace,
  onReplaceAll,
  onClose,
}: SearchReplaceProps) {
  const [searchText, setSearchText] = useState("");
  const [replaceText, setReplaceText] = useState("");
  const [isRegex, setIsRegex] = useState(false);
  const [isCaseSensitive, setIsCaseSensitive] = useState(false);
  const [currentMatchIndex, setCurrentMatchIndex] = useState(0);

  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      // Small delay to ensure the element is rendered
      const t = setTimeout(() => searchRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [open]);

  const matches = useMemo<SearchMatch[]>(() => {
    if (!searchText) return [];

    let regex: RegExp;
    try {
      const pattern = isRegex ? searchText : escapeRegex(searchText);
      const flags = isCaseSensitive ? "g" : "gi";
      regex = new RegExp(pattern, flags);
    } catch {
      return [];
    }

    const result: SearchMatch[] = [];
    cues.forEach((cue, cueIndex) => {
      let match: RegExpExecArray | null;
      regex.lastIndex = 0;
      while ((match = regex.exec(cue.text)) !== null) {
        result.push({
          cueIndex,
          startChar: match.index,
          endChar: match.index + match[0].length,
        });
        // Prevent infinite loops on zero-length matches
        if (match[0].length === 0) {
          regex.lastIndex++;
        }
      }
    });
    return result;
  }, [searchText, cues, isRegex, isCaseSensitive]);

  // Clamp currentMatchIndex when matches change
  useEffect(() => {
    if (matches.length === 0) {
      setCurrentMatchIndex(0);
    } else if (currentMatchIndex >= matches.length) {
      setCurrentMatchIndex(0);
    }
  }, [matches.length, currentMatchIndex]);

  const navigateTo = useCallback(
    (index: number) => {
      if (matches.length === 0) return;
      const wrapped =
        ((index % matches.length) + matches.length) % matches.length;
      setCurrentMatchIndex(wrapped);
      onNavigate(matches[wrapped].cueIndex);
    },
    [matches, onNavigate],
  );

  const goNext = useCallback(() => {
    navigateTo(currentMatchIndex + 1);
  }, [navigateTo, currentMatchIndex]);

  const goPrev = useCallback(() => {
    navigateTo(currentMatchIndex - 1);
  }, [navigateTo, currentMatchIndex]);

  const handleReplace = useCallback(() => {
    if (matches.length === 0) return;
    const m = matches[currentMatchIndex];
    if (!m) return;
    // Pass the exact highlighted offsets so the right occurrence is replaced
    // even when the same text appears multiple times in a cue.
    onReplace(m.cueIndex, m.startChar, m.endChar, replaceText);
  }, [matches, currentMatchIndex, onReplace, replaceText]);

  const handleReplaceAll = useCallback(() => {
    if (!searchText || matches.length === 0) return;
    // Pass the raw search text plus the active flags; the parent builds the
    // RegExp (escaping when not in regex mode and honoring case sensitivity).
    onReplaceAll(searchText, replaceText, isCaseSensitive, isRegex);
  }, [
    searchText,
    matches.length,
    isCaseSensitive,
    isRegex,
    replaceText,
    onReplaceAll,
  ]);

  const handleSearchKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        onClose();
      } else if (e.key === "Enter" && e.shiftKey) {
        e.preventDefault();
        goPrev();
      } else if (e.key === "Enter") {
        e.preventDefault();
        goNext();
      }
    },
    [onClose, goNext, goPrev],
  );

  const handleReplaceKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        onClose();
      }
    },
    [onClose],
  );

  if (!open) return null;

  const counterText =
    matches.length > 0
      ? `${currentMatchIndex + 1} of ${matches.length}`
      : searchText
        ? "No results"
        : "";

  return (
    <div style={styles.container}>
      {/* Search row */}
      <div style={styles.row}>
        <input
          ref={searchRef}
          type="text"
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          onKeyDown={handleSearchKeyDown}
          placeholder="Search..."
          style={styles.input}
          spellCheck={false}
        />
        <span style={styles.counter}>{counterText}</span>
        <button
          type="button"
          style={styles.btn}
          onClick={goPrev}
          title="Previous match (Shift+Enter)"
        >
          &lt;
        </button>
        <button
          type="button"
          style={styles.btn}
          onClick={goNext}
          title="Next match (Enter)"
        >
          &gt;
        </button>
        <button
          type="button"
          style={{
            ...styles.btn,
            ...(isRegex ? styles.btnActive : {}),
          }}
          onClick={() => setIsRegex((v) => !v)}
          title="Toggle regex"
        >
          .*
        </button>
        <button
          type="button"
          style={{
            ...styles.btn,
            ...(isCaseSensitive ? styles.btnActive : {}),
          }}
          onClick={() => setIsCaseSensitive((v) => !v)}
          title="Toggle case sensitivity"
        >
          Aa
        </button>
        <button
          type="button"
          style={styles.btn}
          onClick={onClose}
          title="Close (Escape)"
        >
          &times;
        </button>
      </div>

      {/* Replace row */}
      <div style={styles.row}>
        <input
          type="text"
          value={replaceText}
          onChange={(e) => setReplaceText(e.target.value)}
          onKeyDown={handleReplaceKeyDown}
          placeholder="Replace..."
          style={styles.input}
          spellCheck={false}
        />
        <button
          type="button"
          style={styles.replaceBtn}
          onClick={handleReplace}
          title="Replace current match"
        >
          Replace
        </button>
        <button
          type="button"
          style={styles.replaceBtn}
          onClick={handleReplaceAll}
          title="Replace all matches"
        >
          Replace All
        </button>
      </div>
    </div>
  );
}

export default SearchReplace;
