/**
 * Safe Markdown subset rendered as React elements. Never uses innerHTML: raw HTML
 * in a note is shown as text. Links render only for https: URLs and Hub paths.
 */
import { Fragment, type ReactNode } from "react";

export function safeHref(url: string): string | null {
  if (/^\/(?!\/)[A-Za-z0-9/_.#?=&%-]*$/.test(url)) return url;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Inline: `code`, **bold**, *italic*, [text](url). Everything else is literal text. */
export function inline(text: string, keyPrefix = ""): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyPrefix}${index++}`;
    if (match[1]) out.push(<code key={key}>{token.slice(1, -1)}</code>);
    else if (match[2]) out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    else if (match[3]) out.push(<em key={key}>{token.slice(1, -1)}</em>);
    else {
      const [, label, url] = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token)!;
      const href = safeHref(url);
      out.push(href
        ? <a key={key} href={href} target={href.startsWith("/") ? undefined : "_blank"} rel="noopener noreferrer">{label}</a>
        : `${label} (${url})`);
    }
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      i++;
      blocks.push(<pre key={key++} className="md-code"><code>{code.join("\n")}</code></pre>);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const content = inline(heading[2], `h${key}-`);
      blocks.push(level === 1 ? <h2 key={key++} className="md-h1">{content}</h2>
        : level === 2 ? <h3 key={key++} className="md-h2">{content}</h3>
          : <h4 key={key++} className="md-h3">{content}</h4>);
      i++;
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: ReactNode[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        const raw = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        const task = /^\[( |x|X)\]\s+(.*)$/.exec(raw);
        items.push(<li key={i} className={task ? "md-task" : undefined}>
          {task && <input type="checkbox" checked={task[1] !== " "} readOnly disabled aria-label="Checklist item" />}
          {inline(task ? task[2] : raw, `li${i}-`)}
        </li>);
        i++;
      }
      blocks.push(ordered ? <ol key={key++}>{items}</ol> : <ul key={key++}>{items}</ul>);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(<blockquote key={key++}>{inline(quote.join(" "), `q${key}-`)}</blockquote>);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={key++} />);
      i++;
      continue;
    }
    if (/^\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (row: string) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      const head = cells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      blocks.push(
        <div key={key++} className="md-table"><table>
          <thead><tr>{head.map((cell, c) => <th key={c}>{inline(cell, `th${c}-`)}</th>)}</tr></thead>
          <tbody>{rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell, `td${r}${c}-`)}</td>)}</tr>)}</tbody>
        </table></div>,
      );
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const paragraph: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|>|\s*([-*+]|\d+[.)])\s+|\|)/.test(lines[i])) paragraph.push(lines[i++]);
    if (!paragraph.length) paragraph.push(lines[i++]);
    blocks.push(<p key={key++}>{paragraph.map((text, n) => <Fragment key={n}>{n > 0 && <br />}{inline(text, `p${key}${n}-`)}</Fragment>)}</p>);
  }
  return <div className="md">{blocks}</div>;
}
