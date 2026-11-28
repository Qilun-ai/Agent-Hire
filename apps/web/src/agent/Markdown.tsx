// Small, safe markdown renderer for agent replies: builds React elements, never injects HTML.
// Supports headings, paragraphs, bullet/numbered lists, tables, fenced code, inline code,
// bold, italic and links (http/https only).
import { Fragment, type ReactNode } from "react";
import { safeUrl } from "../lib/url";

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))|(https?:\/\/[^\s)]+)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const key = `${keyBase}-${i++}`;
    const token = match[0];
    if (match[1]) out.push(<code key={key}>{token.slice(1, -1)}</code>);
    else if (match[2]) out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    else if (match[3]) out.push(<em key={key}>{token.slice(1, -1)}</em>);
    else if (match[4]) {
      const label = token.slice(1, token.indexOf("]("));
      out.push(safeUrl(match[5]) ? <a key={key} href={safeUrl(match[5])} target="_blank" rel="noreferrer noopener">{label}</a> : label);
    } else if (match[6]) out.push(safeUrl(token) ? <a key={key} href={safeUrl(token)} target="_blank" rel="noreferrer noopener">{token}</a> : token);
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith("```")) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++]);
      i++;
      blocks.push(<pre key={key++}><code>{body.join("\n")}</code></pre>);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = Math.min(4, heading[1].length + 1);
      const content = inline(heading[2], `h${key}`);
      blocks.push(level === 2 ? <h2 key={key++}>{content}</h2> : level === 3 ? <h3 key={key++}>{content}</h3> : <h4 key={key++}>{content}</h4>);
      i++;
      continue;
    }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(splitRow(lines[i++]));
      blocks.push(
        <div className="md-table-wrap" key={key++}>
          <table>
            <thead><tr>{head.map((cell, c) => <th key={c}>{inline(cell, `th${key}-${c}`)}</th>)}</tr></thead>
            <tbody>{rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell, `td${key}-${r}-${c}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, ""));
        i++;
      }
      const children = items.map((item, n) => <li key={n}>{inline(item, `li${key}-${n}`)}</li>);
      blocks.push(ordered ? <ol key={key++}>{children}</ol> : <ul key={key++}>{children}</ul>);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*([-*]|\d+[.)])\s+|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) { para.push(lines[i++]); }
    blocks.push(
      <p key={key++}>
        {para.map((p, n) => <Fragment key={n}>{n > 0 && <br />}{inline(p, `p${key}-${n}`)}</Fragment>)}
      </p>,
    );
  }
  return <div className="md">{blocks}</div>;
}
