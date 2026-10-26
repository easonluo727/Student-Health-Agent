import type { Integrations, Plan, ServerEvent, TurnRequest, UploadResult } from "./protocol";
import { API_BASE, MAX_UPLOAD_BYTES, UPLOAD_TYPES } from "./config";
import { readServerEvents } from "./sse";
import { createMockTransport } from "./mock/transport";

export interface AgentTransport {
  getPlans(): Promise<Plan[]>;
  getIntegrations(): Promise<Integrations>;
  upload(sessionId: string, file: File): Promise<UploadResult>;
  turn(request: TurnRequest, signal?: AbortSignal): AsyncIterable<ServerEvent>;
}

export function validateUpload(file: File): string | null {
  const heic = /\.(heic|heif)$/i.test(file.name);
  if (!UPLOAD_TYPES.includes(file.type) && !heic) return `${file.name}: only PDF, JPEG, PNG, WebP or HEIC files can be attached.`;
  if (file.size > MAX_UPLOAD_BYTES) return `${file.name} is larger than 25 MB.`;
  return null;
}

async function failure(res: Response): Promise<Error> {
  let detail = "";
  try { detail = await res.text(); } catch { /* ignore */ }
  try { const j = JSON.parse(detail) as { detail?: unknown; message?: unknown }; detail = String(j.detail ?? j.message ?? detail); } catch { /* plain text */ }
  return new Error(`Request failed (${res.status})${detail ? `: ${detail}` : ""}`);
}

export function createHttpTransport(base: string): AgentTransport {
  return {
    async getPlans() {
      const res = await fetch(`${base}/plans`);
      if (!res.ok) throw await failure(res);
      return res.json();
    },
    async getIntegrations() {
      const res = await fetch(`${base}/integrations`);
      if (!res.ok) throw await failure(res);
      return res.json();
    },
    async upload(sessionId, file) {
      const problem = validateUpload(file);
      if (problem) throw new Error(problem);
      const form = new FormData();
      form.append("session_id", sessionId);
      form.append("file", file);
      const res = await fetch(`${base}/upload`, { method: "POST", body: form });
      if (!res.ok) throw await failure(res);
      return res.json();
    },
    async *turn(request, signal) {
      const res = await fetch(`${base}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(request),
        signal: signal ?? null,
      });
      if (!res.ok || !res.body) throw await failure(res);
      yield* readServerEvents(res.body, signal);
    },
  };
}

/** Real API when a base URL is configured, otherwise the scripted mock. */
export function createTransport(): AgentTransport {
  return API_BASE ? createHttpTransport(API_BASE) : createMockTransport();
}
