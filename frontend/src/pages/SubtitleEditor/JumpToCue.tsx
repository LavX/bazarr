import { useCallback, useEffect, useRef, useState } from "react";

interface JumpToCueProps {
  open: boolean;
  cueCount: number;
  onJump: (index: number) => void;
  onClose: () => void;
}

const containerStyle: React.CSSProperties = {
  position: "absolute",
  top: 8,
  left: "50%",
  transform: "translateX(-50%)",
  zIndex: 10,
  minWidth: 240,
  background: "var(--bz-surface-raised)",
  border: "1px solid var(--bz-border-card)",
  borderRadius: "var(--bz-radius-sm)",
  padding: "8px 12px",
  boxShadow: "var(--bz-shadow-float)",
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const labelStyle: React.CSSProperties = {
  fontSize: 13,
  color: "var(--bz-text-secondary)",
  whiteSpace: "nowrap",
  flexShrink: 0,
};

const inputStyle: React.CSSProperties = {
  flex: 1,
  fontFamily: "'JetBrains Mono', monospace",
  fontSize: 13,
  background: "var(--bz-surface-base)",
  border: "1px solid var(--bz-border-interactive)",
  color: "var(--bz-text-primary)",
  borderRadius: 4,
  padding: "6px 8px",
  outline: "none",
  width: 80,
};

const hintStyle: React.CSSProperties = {
  fontSize: 11,
  color: "var(--bz-text-disabled)",
  whiteSpace: "nowrap",
  flexShrink: 0,
};

export default function JumpToCue({
  open,
  cueCount,
  onJump,
  onClose,
}: JumpToCueProps) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setValue("");
      const t = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [open]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "Enter") {
        e.preventDefault();
        const num = parseInt(value.trim());
        if (!isNaN(num) && num >= 1 && num <= cueCount) {
          onJump(num - 1);
          onClose();
        }
      }
    },
    [value, cueCount, onJump, onClose],
  );

  if (!open) return null;

  const num = parseInt(value.trim());
  const isValid = !value.trim() || (!isNaN(num) && num >= 1 && num <= cueCount);

  return (
    <div style={containerStyle}>
      <span style={labelStyle}>Go to cue #</span>
      <input
        ref={inputRef}
        type="number"
        min={1}
        max={cueCount}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        style={{
          ...inputStyle,
          borderColor: isValid ? "var(--bz-border-interactive)" : "#EF4444",
        }}
        placeholder={`1\u2013${cueCount}`}
      />
      <span style={hintStyle}>Enter to jump</span>
    </div>
  );
}
