import { useEffect, useRef, useState } from "react";
import { ArrowUp, ChevronDown, FileText, Image as ImageIcon, Loader2, Paperclip, Square, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { UPLOAD_ACCEPT } from "@/agent/config";
import type { Agent } from "@/agent/useAgent";
import type { LocalAttachment } from "@/agent/store";
import { validateUpload } from "@/agent/transport";

type Pending = { key: string; name: string; image: boolean; preview?: string | undefined; result?: LocalAttachment | undefined; error?: string | undefined };

export function Composer({ agent, inputRef }: { agent: Agent; inputRef: React.RefObject<HTMLTextAreaElement | null> }) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Pending[]>([]);
  const picker = useRef<HTMLInputElement>(null);
  const plansDropdown = useRef<HTMLDetailsElement>(null);
  const busy = agent.state.busy;
  const uploading = files.some(f => !f.result && !f.error);
  const ready = files.filter(f => f.result).map(f => f.result!);
  const canSend = !busy && !uploading && (!!text.trim() || ready.length > 0);
  useEffect(() => () => files.forEach(f => f.preview && URL.revokeObjectURL(f.preview)), []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    function dismiss(event: PointerEvent) {
      const dropdown = plansDropdown.current;
      if (dropdown && !dropdown.contains(event.target as Node)) dropdown.open = false;
    }
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, []);

  function add(list: File[]) {
    for (const file of list) {
      const key = `${file.name}-${file.size}-${Math.random()}`;
      const image = file.type.startsWith("image/") || /\.(heic|heif)$/i.test(file.name);
      const problem = validateUpload(file);
      const preview = image && !problem && /jpe?g|png|webp/i.test(file.type) ? URL.createObjectURL(file) : undefined;
      setFiles(prev => [...prev, { key, name: file.name, image, preview, error: problem ?? undefined }]);
      if (problem) continue;
      agent.upload(file)
        .then(r => setFiles(prev => prev.map(p => p.key === key ? { ...p, result: { ...r, preview } } : p)))
        .catch(e => setFiles(prev => prev.map(p => p.key === key ? { ...p, error: e instanceof Error ? e.message : "Upload failed" } : p)));
    }
  }
  function send() {
    if (!canSend) return;
    void agent.sendMessage(text.trim(), ready);
    setText(""); setFiles([]);
  }
  const t = agent.replyTarget;
  return <>
    <form className="composer" onSubmit={e => { e.preventDefault(); send(); }}>
      {t && <div className="mb-2"><span className="attachment-tag">Replying about: {t.label}<Button type="button" variant="ghost" size="icon" className="size-5" aria-label="Clear reply target" onClick={() => agent.setReplyTarget(null)}><X /></Button></span></div>}
      <textarea ref={inputRef} aria-label="Message" title="Drag the bottom-right corner to resize" placeholder="Ask a question…" value={text} onChange={e => setText(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <Button type="button" variant="ghost" size="icon" className="rounded-full" aria-label="Attach plan PDF or bill photo" title="Attach plan PDF or bill photo" onClick={() => picker.current?.click()}><Paperclip /></Button>
          <select aria-label="Mode" className="composer-mode" value={agent.mode} onChange={e => agent.setMode(e.target.value as Agent["mode"])}>
            <option value="chat">Just chat</option><option value="calculate">Calculate</option><option value="review">Review</option>
          </select>
        </div>
        {busy ? <Button type="button" size="icon" className="send-button" aria-label="Stop" title="Stop" onClick={agent.stop}><Square /></Button>
          : <Button type="submit" size="icon" className="send-button" aria-label="Send message" title="Send message" disabled={!canSend}><ArrowUp /></Button>}
      </div>
      {agent.mode !== "chat" && agent.plans.length > 0 && <details ref={plansDropdown} className="plan-picker"
        onKeyDown={e => { if (e.key === "Escape") { e.preventDefault(); e.currentTarget.open = false; e.currentTarget.querySelector("summary")?.focus(); } }}>
        <summary><span>{agent.planIds.length ? `${agent.planIds.length} ${agent.planIds.length === 1 ? "plan" : "plans"} selected` : "Select plans"}</span><ChevronDown aria-hidden className="size-4" /></summary>
        <div className="plan-picker-menu">
          <div className="plan-picker-heading"><span>Choose plans to {agent.mode === "review" ? "compare" : "calculate"}</span>
            <button type="button" onClick={() => { if (plansDropdown.current) { plansDropdown.current.open = false; plansDropdown.current.querySelector("summary")?.focus(); } }}>Done</button>
          </div>
          <div className="plan-picker-options" role="group" aria-label="Plans">{agent.plans.map(p => {
            const selected = agent.planIds.includes(p.plan_id);
            return <label key={p.plan_id} className={`plan-picker-option ${selected ? "selected" : ""}`}>
              <input type="checkbox" checked={selected} onChange={() => agent.setPlanIds(selected ? agent.planIds.filter(x => x !== p.plan_id) : [...agent.planIds, p.plan_id])} />
              <span>{p.name}{p.uploaded && <em className="unverified-plan">Uploaded · unverified</em>}<small>{p.plan_year}</small></span>
            </label>;
          })}</div>
        </div>
      </details>}
      <input ref={picker} className="hidden" type="file" multiple accept={UPLOAD_ACCEPT} onChange={e => { add(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
    </form>
    {files.length > 0 && <div className="mt-2 flex flex-wrap gap-2">{files.map(f => <span className={`attachment-tag ${f.error ? "attachment-error" : ""}`} key={f.key} title={f.error}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      {f.preview ? <img src={f.preview} alt="" className="attachment-thumb" /> : f.image ? <ImageIcon className="size-4" /> : <FileText className="size-4" />}
      {f.name}{!f.result && !f.error && <Loader2 className="size-3.5 animate-spin" />}{f.error && <span className="text-xs">· {f.error.replace(`${f.name}: `, "").replace(f.name, "File")}</span>}
      <Button variant="ghost" size="icon" className="size-5" aria-label={`Remove ${f.name}`} onClick={() => setFiles(prev => prev.filter(p => p.key !== f.key))}><X /></Button>
    </span>)}</div>}
  </>;
}
