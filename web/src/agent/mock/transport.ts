import type { AgentTransport } from "../transport";
import { validateUpload } from "../transport";
import type { Integrations, ServerEvent } from "../protocol";
import { bill, comparison, emailSent, mockPlans, say, scriptForMessage } from "./scripts";

const wait = (ms: number, signal?: AbortSignal) => new Promise<void>(resolve => {
  if (!ms) return resolve();
  const t = setTimeout(resolve, ms);
  signal?.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
});

/** Mock backend that replays scripted events. `delay` is ms between events. */
export function createMockTransport({ delay = 35 }: { delay?: number } = {}): AgentTransport {
  const uploads = new Map<string, { name: string; image: boolean }>();
  let n = 0;
  const integrations: Integrations = { gmail: { connected: true, email: "you@gmail.com", connect_url: null } };
  return {
    async getPlans() { return mockPlans; },
    async getIntegrations() { return integrations; },
    async upload(_session, file) {
      const problem = validateUpload(file);
      if (problem) throw new Error(problem);
      const file_id = `mock-file-${++n}`;
      const image = file.type.startsWith("image/") || /\.(heic|heif)$/i.test(file.name);
      uploads.set(file_id, { name: file.name, image });
      return { file_id, name: file.name, size: file.size, content_type: file.type || (image ? "image/heic" : "application/pdf") };
    },
    async *turn(req, signal) {
      const input = req.input;
      let events: ServerEvent[];
      if (input.type === "message") {
        const image = input.attachments?.find(a => uploads.get(a.file_id)?.image);
        const script = scriptForMessage(input.text, !!image);
        events = image ? bill(image.file_id, image.name) : script ? script() : [
          ...say(`m-${Date.now()}`, "This preview is running without the agent server, so it can only replay the sample scenarios: ask about a specialist copay, comparing 2027 plans, finding an asthma specialist, or attach a bill photo."),
          { event: "done", data: {} },
        ];
      } else if (input.type === "form_response") events = comparison();
      else if (input.type === "email_response") events = input.action === "send"
        ? emailSent(input.request_id, input.draft?.to ?? [])
        : [...say(`m-${Date.now()}`, "Okay, I won't send it."), { event: "done", data: {} }];
      else events = [...say(`m-${Date.now()}`, "Got it."), { event: "done", data: {} }];
      for (const ev of events) {
        if (signal?.aborted) return;
        await wait(ev.event === "message_delta" ? delay / 3 : delay, signal);
        yield ev;
      }
    },
  };
}
