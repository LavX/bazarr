import type { Cue } from "@/pages/SubtitleEditor/types";
import { isCombinedOutputLanguageKey } from "@/utilities/subtitles";
import type { SubtitleFormat } from "./types";

// A combined ASS output stores each language as its own positioned Dialogue
// (Bottom = primary, Top/Middle = secondaries) at the SAME timestamp, so the
// ASS parser imports each language as a separate cue. That recreates the
// "two overlapping cues per timestamp" view combined editing is meant to avoid.
//
// For combined ASS subtitles we merge those same-timestamp dialogues into one
// stacked cue for editing (primary line(s) first, then each secondary), and we
// remember each dialogue's original line so we can split the cue back into the
// positioned dialogues on save without losing placement. SRT combined output is
// already one stacked cue, so it never needs this.

interface StackMember {
  // The original `Dialogue:` line, so the serializer can patch text/timing back
  // into it and keep the Style/position. Undefined for a member with no source.
  rawText?: string;
  // How many display lines this dialogue contributed at merge time, used to
  // split the edited cue text back across the dialogues.
  lineCount: number;
}

interface CombinedStackMeta {
  combinedStack: StackMember[];
}

export function isCombinedAssSubtitle(
  language: string | undefined,
  format: SubtitleFormat | undefined,
): boolean {
  return (
    (format === "ass" || format === "ssa") &&
    isCombinedOutputLanguageKey(language)
  );
}

function lineCountOf(text: string): number {
  return text.length === 0 ? 0 : text.split("\n").length;
}

/**
 * Merge runs of consecutive cues that share an identical start/end (the
 * positioned dialogues of one combined cue) into a single stacked cue.
 */
export function mergeCombinedAssCues(cues: Cue[]): Cue[] {
  const merged: Cue[] = [];
  let i = 0;
  while (i < cues.length) {
    const { startMs, endMs } = cues[i];
    let j = i + 1;
    while (
      j < cues.length &&
      cues[j].startMs === startMs &&
      cues[j].endMs === endMs
    ) {
      j += 1;
    }
    const group = cues.slice(i, j);
    if (group.length <= 1) {
      merged.push(group[0]);
    } else {
      const members: StackMember[] = group.map((c) => ({
        rawText: c.rawText,
        lineCount: lineCountOf(c.text),
      }));
      merged.push({
        id: group[0].id,
        startMs,
        endMs,
        text: group.map((c) => c.text).join("\n"),
        formatMetadata: { combinedStack: members } satisfies CombinedStackMeta,
      });
    }
    i = j;
  }
  return merged;
}

/**
 * Inverse of mergeCombinedAssCues: split each stacked cue back into one cue per
 * original dialogue, distributing the edited text lines by each member's stored
 * line count (the last member absorbs any added/removed lines). Empty members
 * are dropped so a deleted language line does not leave a blank dialogue.
 */
export function splitCombinedAssCues(cues: Cue[]): Cue[] {
  const out: Cue[] = [];
  for (const cue of cues) {
    const meta = cue.formatMetadata as CombinedStackMeta | undefined;
    const stack = meta?.combinedStack;
    if (!Array.isArray(stack) || stack.length === 0) {
      out.push(cue);
      continue;
    }
    const lines = cue.text.split("\n");
    let cursor = 0;
    stack.forEach((member, idx) => {
      const isLast = idx === stack.length - 1;
      const remaining = lines.length - cursor;
      const take = isLast ? remaining : Math.min(member.lineCount, remaining);
      const chunk = lines.slice(cursor, cursor + Math.max(0, take));
      cursor += Math.max(0, take);
      const text = chunk.join("\n");
      if (text.length === 0) return;
      out.push({
        id: idx === 0 ? cue.id : `${cue.id}-stack-${idx}`,
        startMs: cue.startMs,
        endMs: cue.endMs,
        text,
        rawText: member.rawText,
      });
    });
  }
  return out;
}
