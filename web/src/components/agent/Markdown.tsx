import { Fragment, type ReactNode } from "react";

// Minimal, safe Markdown subset (no raw HTML): paragraphs, line breaks, #/##/### headings,
// - / 1. lists (also after a lead-in line), GFM tables, **bold**, *em*, `code`, [link](https://…).
function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0, m: RegExpExecArray | null, i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${i++}`;
    if (m[1]) out.push(<strong key={k}>{m[1]}</strong>);
    else if (m[2]) out.push(<em key={k}>{m[2]}</em>);
    else if (m[3]) out.push(<code key={k}>{m[3]}</code>);
    else out.push(<a key={k} href={m[5]} target="_blank" rel="noreferrer" className="underline underline-offset-2">{m[4]}</a>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const ul = /^\s*[-*+]\s+/, ol = /^\s*\d+[.)]\s+/, heading = /^\s*(#{1,6})\s+(.*?)\s*#*\s*$/;
const isRow = (l: string) => /^\s*\|.*\|\s*$/.test(l) || (l.includes("|") && l.trim().length > 1);
const isSep = (l: string) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(l);
const cells = (l: string) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));

type Block =
  | { t: "p"; lines: string[] } | { t: "h"; level: number; text: string }
  | { t: "ul" | "ol"; items: string[] } | { t: "table"; head: string[]; align: ("left" | "right" | "center" | undefined)[]; rows: string[][] };

export function parseBlocks(text: string): Block[] {
  const src = text.replace(/\r\n?/g, "\n").split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < src.length) {
    const l = src[i]!;
    if (!l.trim()) { i++; continue; }
    const h = heading.exec(l);
    if (h) { out.push({ t: "h", level: h[1]!.length, text: h[2]! }); i++; continue; }
    if (isRow(l) && i + 1 < src.length && isSep(src[i + 1]!)) {
      const head = cells(l);
      const align = cells(src[i + 1]!).map(c => c.startsWith(":") && c.endsWith(":") ? "center" as const : c.endsWith(":") ? "right" as const : c.startsWith(":") ? "left" as const : undefined);
      const rows: string[][] = [];
      i += 2;
      while (i < src.length && src[i]!.trim() && src[i]!.includes("|")) rows.push(cells(src[i++]!));
      out.push({ t: "table", head, align, rows });
      continue;
    }
    const list = ul.test(l) ? "ul" : ol.test(l) ? "ol" : null;
    if (list) {
      const re = list === "ul" ? ul : ol, items: string[] = [];
      while (i < src.length && re.test(src[i]!)) items.push(src[i++]!.replace(re, ""));
      out.push({ t: list, items });
      continue;
    }
    const lines: string[] = [];
    while (i < src.length && src[i]!.trim() && !heading.test(src[i]!) && !ul.test(src[i]!) && !ol.test(src[i]!)
      && !(isRow(src[i]!) && i + 1 < src.length && isSep(src[i + 1]!))) lines.push(src[i++]!);
    out.push({ t: "p", lines });
  }
  return out;
}

export function Markdown({ text }: { text: string }) {
  return <>{parseBlocks(text).map((b, bi) => {
    const k = String(bi), mt = bi > 0 ? "mt-3" : undefined;
    switch (b.t) {
      case "h": return <p key={k} role="heading" aria-level={Math.min(b.level + 1, 6)} className={`${mt ?? ""} text-base font-semibold`}>{inline(b.text, k)}</p>;
      case "ul": return <ul key={k} className="my-2 list-disc pl-5">{b.items.map((r, i) => <li key={i}>{inline(r, `${k}-${i}`)}</li>)}</ul>;
      case "ol": return <ol key={k} className="my-2 list-decimal pl-5">{b.items.map((r, i) => <li key={i}>{inline(r, `${k}-${i}`)}</li>)}</ol>;
      case "table": return <div key={k} className={`${mt ?? ""} overflow-x-auto max-w-full`}><table className="data-table">
        <thead><tr>{b.head.map((c, i) => <th key={i} style={b.align[i] ? { textAlign: b.align[i] } : undefined}>{inline(c, `${k}-h${i}`)}</th>)}</tr></thead>
        <tbody>{b.rows.map((r, ri) => <tr key={ri}>{b.head.map((_, ci) => <td key={ci} style={b.align[ci] ? { textAlign: b.align[ci] } : undefined}>{inline(r[ci] ?? "", `${k}-${ri}-${ci}`)}</td>)}</tr>)}</tbody>
      </table></div>;
      case "p": return <p key={k} className={mt}>{b.lines.map((l, i) => <Fragment key={i}>{i > 0 && <br />}{inline(l, `${k}-${i}`)}</Fragment>)}</p>;
    }
  })}</>;
}
