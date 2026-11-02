import type { ServerEvent } from "./protocol";

export interface RawSseEvent { event: string; data: string }

/** Incremental text/event-stream parser. Feed decoded chunks; returns completed events. */
export class SseParser {
  private buffer = "";
  private eventName = "";
  private data: string[] = [];

  push(chunk: string): RawSseEvent[] {
    this.buffer += chunk;
    const out: RawSseEvent[] = [];
    let idx: number;
    while ((idx = this.buffer.search(/\r\n|\r|\n/)) !== -1) {
      const line = this.buffer.slice(0, idx);
      const nlLen = this.buffer.startsWith("\r\n", idx) ? 2 : 1;
      // A lone trailing "\r" may be the first half of "\r\n": wait for more input.
      if (nlLen === 1 && this.buffer[idx] === "\r" && idx === this.buffer.length - 1) break;
      this.buffer = this.buffer.slice(idx + nlLen);
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** Flush at end of stream (dispatches a final event without trailing blank line). */
  end(): RawSseEvent[] {
    const out: RawSseEvent[] = [];
    if (this.buffer) { const ev = this.line(this.buffer); this.buffer = ""; if (ev) out.push(ev); }
    const ev = this.line("");
    if (ev) out.push(ev);
    return out;
  }

  private line(line: string): RawSseEvent | null {
    if (line === "") {
      if (!this.data.length) { this.eventName = ""; return null; }
      const ev = { event: this.eventName || "message", data: this.data.join("\n") };
      this.eventName = ""; this.data = [];
      return ev;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.eventName = value;
    else if (field === "data") this.data.push(value);
    return null;
  }
}

const KNOWN = new Set(["message_start", "message_delta", "message_end", "form", "confirm", "estimate", "search_step", "report", "email_draft", "email_status", "page", "error", "done"]);

export function toServerEvent(raw: RawSseEvent): ServerEvent | null {
  if (!KNOWN.has(raw.event)) return null;
  let data: unknown;
  try { data = raw.data ? JSON.parse(raw.data) : {}; } catch { return { event: "error", data: { message: `Malformed ${raw.event} event from the server.` } }; }
  return { event: raw.event, data } as ServerEvent;
}

/** Read a fetch Response body as a stream of typed server events. */
export async function* readServerEvents(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<ServerEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  try {
    while (true) {
      if (signal?.aborted) return;
      const { value, done } = await reader.read();
      const raws = done ? [...parser.push(decoder.decode()), ...parser.end()] : parser.push(decoder.decode(value, { stream: true }));
      for (const raw of raws) { const ev = toServerEvent(raw); if (ev) yield ev; }
      if (done) return;
    }
  } finally {
    reader.releaseLock();
  }
}
