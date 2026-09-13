import { Fragment, type ReactNode } from "react";

interface Format {
  tag: string;
  color?: string;
}

// Provider tags never become HTML. Only these formatting choices reach React.
export default function SubtitleCueText({ text }: { text: string }) {
  const formats: Format[] = [];
  const parts: ReactNode[] = [];
  const tokens = /<\/?[a-z][a-z0-9-]*(?=[\s/>])[^<>]*>|\{\\[^}]*\}/gi;
  const append = (value: string) => {
    if (!value) return;
    let content: ReactNode = value;
    for (let i = formats.length - 1; i >= 0; i -= 1) {
      const format = formats[i];
      if (format.tag === "i") content = <i>{content}</i>;
      if (format.tag === "b") content = <b>{content}</b>;
      if (format.tag === "u") content = <u>{content}</u>;
      if (format.tag === "s") content = <s>{content}</s>;
      if (format.tag === "font" && format.color)
        content = <span style={{ color: format.color }}>{content}</span>;
    }
    parts.push(<Fragment key={parts.length}>{content}</Fragment>);
  };
  let cursor = 0;
  for (const match of text.matchAll(tokens)) {
    append(text.slice(cursor, match.index));
    cursor = match.index + match[0].length;
    const tag = /^<(\/?)\s*([a-z]+)/i.exec(match[0]);
    if (!tag) continue;
    const name = tag[2].toLowerCase();
    if (name === "br") {
      append("\n");
    } else if (["i", "b", "u", "s", "font"].includes(name)) {
      if (tag[1]) {
        const index = formats.findLastIndex((format) => format.tag === name);
        if (index >= 0) formats.splice(index);
      } else if (formats.length < 16) {
        const color = /\bcolor\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i.exec(
          match[0],
        );
        const value = (color?.[1] ?? color?.[2] ?? color?.[3] ?? "").trim();
        formats.push({
          tag: name,
          color: /^(?:#[\da-f]{3}|#[\da-f]{6}|#[\da-f]{8}|[a-z]{1,24})$/i.test(
            value,
          )
            ? value
            : undefined,
        });
      }
    }
  }
  append(text.slice(cursor));
  return <>{parts}</>;
}
