import { useEffect, useRef, useState } from "react";
import { AlertCircle, Check, ChevronDown, ChevronRight, Copy, FileText, Image as ImageIcon, Loader2, Mail, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Answer, AnswerValue, Confirm, EmailDraft, EmailDraftEdit, EmailStatus, Form, Integrations, Question, SearchStep } from "@/agent/protocol";
import { DONT_KNOW, summarizeAnswers } from "@/agent/answers";
import { SourceChips } from "./SourceChip";


// ---------- Questionnaire ----------
type Draft = Record<string, { value: AnswerValue; unknown: boolean }>;

function initialDraft(form: Form): Draft {
  return Object.fromEntries(form.questions.map(q => [q.field, { value: q.prefill?.value ?? (q.kind === "multi" ? [] : null), unknown: false }]));
}

function QuestionField({ q, value, onChange }: { q: Question; value: Draft[string]; onChange: (v: Draft[string]) => void }) {
  const unknownOpt = <label className={`form-option ${value.unknown ? "selected" : ""}`}>
    <input type={q.kind === "multi" || q.kind === "number" || q.kind === "text" ? "checkbox" : "radio"} name={q.field} checked={value.unknown}
      onChange={() => onChange({ value: value.unknown ? value.value : q.kind === "multi" ? [] : null, unknown: !value.unknown })} />{DONT_KNOW}
  </label>;
  const prefillNote = q.prefill && <p className="mt-2 text-xs text-muted-foreground">From your message, please confirm</p>;
  if (q.kind === "multi" || q.kind === "single") {
    const many = q.kind === "multi";
    const selected = Array.isArray(value.value) ? value.value : value.value != null ? [String(value.value)] : [];
    const grid = !many && (q.options?.length ?? 0) <= 4 ? "grid grid-cols-2 gap-2" : "";
    return <fieldset className="question-block"><legend>{q.label}</legend>{q.help && <p className="question-note">{q.help}</p>}
      <div className={grid}>
        {q.options?.map(o => <label key={o.value} className={`form-option ${!value.unknown && selected.includes(o.value) ? "selected" : ""}`}>
          <input type={many ? "checkbox" : "radio"} name={q.field} value={o.value} checked={!value.unknown && selected.includes(o.value)}
            onChange={() => onChange(many ? { value: selected.includes(o.value) ? selected.filter(v => v !== o.value) : [...selected, o.value], unknown: false } : { value: o.value, unknown: false })} />{o.label}
        </label>)}
        {unknownOpt}
      </div>{prefillNote}
    </fieldset>;
  }
  if (q.kind === "weight") {
    return <fieldset className="question-block"><legend>{q.label}</legend>{q.help && <p className="question-note">{q.help}</p>}
      <div className="grid grid-cols-5 gap-2">{[1, 2, 3, 4, 5].map(n => <label key={n} className={`form-option justify-center ${!value.unknown && value.value === n ? "selected" : ""}`}>
        <input type="radio" name={q.field} checked={!value.unknown && value.value === n} onChange={() => onChange({ value: n, unknown: false })} />{n}</label>)}</div>
      {q.scale_labels && <div className="mt-2 flex justify-between text-xs text-muted-foreground"><span>{q.scale_labels[0]}</span><span>{q.scale_labels[1]}</span></div>}
      <div className="mt-2">{unknownOpt}</div>{prefillNote}
    </fieldset>;
  }
  return <label className="question-block block font-semibold">{q.label}{q.help && <span className="question-note block font-normal">{q.help}</span>}
    {q.unit && <span className="mt-3 block text-sm font-normal">{q.unit}</span>}
    <input aria-label={q.unit ?? q.label} className="budget-input" type={q.kind === "number" ? "number" : "text"} min={q.min} max={q.max} disabled={value.unknown}
      value={value.value == null ? "" : String(value.value)}
      onChange={e => onChange({ value: q.kind === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value, unknown: false })} />
    <span className="mt-2 block font-normal">{unknownOpt}</span>{prefillNote}
  </label>;
}

export function FormCard({ form, state, summary, disabled, onSubmit, onDismiss }: { form: Form; state: "open" | "submitted" | "dismissed"; summary?: string | undefined; disabled?: boolean | undefined; onSubmit: (answers: Record<string, Answer>, summary: string) => void; onDismiss: () => void }) {
  const [draft, setDraft] = useState<Draft>(() => initialDraft(form));
  if (state !== "open") return <p className="form-summary"><Check className="size-4 text-positive" />{state === "submitted" ? `${form.title}: ${summary}` : `${form.title} · skipped`}</p>;
  return <form className="question-card" aria-label={form.title} onSubmit={e => {
    e.preventDefault();
    // Empty answers are sent as "unknown" rather than guessed.
    const answers: Record<string, Answer> = Object.fromEntries(form.questions.map(q => {
      const d = draft[q.field]!;
      const empty = d.value == null || d.value === "" || (Array.isArray(d.value) && !d.value.length);
      return [q.field, d.unknown || empty ? { value: null, unknown: true } : { value: d.value, unknown: false }];
    }));
    onSubmit(answers, summarizeAnswers(form, answers));
  }}>
    {form.intro && <p className="question-note mb-4">{form.intro}</p>}
    {form.questions.map((q, i) => {
      // A heading shows once, above the first question of a run that shares it.
      const previous = form.questions.slice(0, i).findLast(p => p.heading)?.heading;
      const heading = q.heading && q.heading !== previous ? q.heading : null;
      return <div key={q.field} className={i > 0 ? "mt-6" : undefined}>
        {heading && <h3 className="mb-3 text-sm font-semibold">{heading}</h3>}
        <QuestionField q={q} value={draft[q.field]!} onChange={v => setDraft(d => ({ ...d, [q.field]: v }))} />
      </div>;
    })}
    <div className="mt-5 flex justify-end gap-2">
      <Button type="button" variant="ghost" className="action-button" disabled={disabled} onClick={onDismiss}>Skip</Button>
      <Button type="submit" className="action-button" disabled={disabled}>Continue</Button>
    </div>
  </form>;
}

// ---------- Confirm ----------
const sourceKindLabel: Record<string, string> = { policy: "Plan document", mrf: "Price file", web: "Web", user_form: "Your answers", user_message: "Your message", inferred: "Inferred" };
export function ConfirmCard({ confirm, state, correction, disabled, onRespond }: { confirm: Confirm; state: "open" | "confirmed" | "corrected"; correction?: string | undefined; disabled?: boolean | undefined; onRespond: (ok: boolean, correction?: string) => void }) {
  const [off, setOff] = useState(false);
  const [text, setText] = useState("");
  if (state !== "open") return <p className="form-summary"><Check className="size-4 text-positive" />{confirm.title}: {state === "confirmed" ? "Looks right" : `Corrected — ${correction}`}</p>;
  return <div className="question-card" aria-label={confirm.title}>
    <h3 className="font-semibold">{confirm.title}</h3>
    <dl className="mt-4 space-y-3">{confirm.items.map(it => <div key={it.label} className="flex items-start justify-between gap-4 border-b border-border pb-3 text-sm">
      <dt className="text-body">{it.label}<span className="mt-1 block text-xs text-muted-foreground">{sourceKindLabel[it.source]}</span></dt><dd className="text-right font-semibold">{it.value}</dd></div>)}</dl>
    {off && <textarea aria-label="What's off?" className="letter-editor mt-4 min-h-24" placeholder="Tell me what's wrong" value={text} onChange={e => setText(e.target.value)} />}
    <div className="mt-5 flex justify-end gap-2">
      {off ? <Button className="action-button" disabled={disabled || !text.trim()} onClick={() => onRespond(false, text.trim())}>Send correction</Button> : <>
        <Button variant="ghost" className="action-button" disabled={disabled} onClick={() => setOff(true)}>Something&apos;s off</Button>
        <Button className="action-button" disabled={disabled} onClick={() => onRespond(true)}>Looks right</Button></>}
    </div>
  </div>;
}

// ---------- Search steps ----------
export function SearchSteps({ steps, onOpen }: { steps: SearchStep[]; onOpen?: (() => void) | undefined }) {
  const [expanded, setExpanded] = useState(false);
  const running = steps.some(s => s.status === "running");
  const web = steps.some(s => s.kind === "web");
  const summary = steps.length === 1 && steps[0]!.title ? steps[0]!.title
    : web ? `Searched the web · ${steps.filter(s => s.url).length} pages` : `What I did · ${steps.length} steps`;
  const open = expanded || running;
  return <div className="my-6">
    <Button variant="ghost" className="step-summary" aria-expanded={open} onClick={() => setExpanded(v => !v)}>{open ? <ChevronDown /> : <ChevronRight />}{summary}</Button>
    {open && <div className="mt-3 space-y-2">{steps.map(s => <Button variant="ghost" className="step-line" key={s.step_id} onClick={() => web && onOpen?.()}>
      {s.status === "running" ? <Loader2 className="animate-spin" /> : s.status === "failed" ? <AlertCircle className="step-failed" /> : <Check className="text-positive" />}{s.title && web ? s.title : s.text}
    </Button>)}{web && onOpen && <Button variant="ghost" className="step-line" onClick={onOpen}><ChevronRight />Open search details</Button>}</div>}
  </div>;
}

// ---------- Email ----------
const splitList = (s: string) => s.split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
function fmtTime(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function EmailCard({ draft, status, state, disabled, integrations, onRefreshIntegrations, onSend, onCancel }: {
  draft: EmailDraft; status?: EmailStatus | undefined; state: "open" | "cancelled"; disabled?: boolean | undefined; integrations: Integrations | null;
  onRefreshIntegrations: () => Promise<Integrations | null>; onSend: (d: EmailDraftEdit) => void; onCancel: () => void;
}) {
  const [to, setTo] = useState(draft.to.join(", "));
  const [cc, setCc] = useState((draft.cc ?? []).join(", "));
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.body);
  const [connecting, setConnecting] = useState(false);
  const [copied, setCopied] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);
  const from = draft.from ?? (integrations?.gmail.connected ? integrations.gmail.email : null);

  if (state === "cancelled") return <p className="form-summary"><Mail className="size-4" />{draft.title} · not sent</p>;
  if (status?.status === "sent") return <p className="form-summary"><Check className="size-4 text-positive" />{`Sent to ${splitList(to).join(", ")}${status.sent_at ? ` at ${fmtTime(status.sent_at)}` : ""}${status.message ? ` · ${status.message}` : ""}`}</p>;

  const sending = status?.status === "sending";
  const locked = disabled || sending;
  const edit = (): EmailDraftEdit => ({ to: splitList(to), cc: splitList(cc), subject, body });
  // Gmail sending isn't built: the user sends the letter from their own email.
  async function copy() {
    await navigator.clipboard.writeText(`Subject: ${subject}\n\n${body}`);
    setCopied(true);
  }
  function connect() {
    const url = integrations?.gmail.connect_url;
    if (!url) { void onRefreshIntegrations(); return; }
    const popup = window.open(url, "connect-gmail", "width=520,height=680");
    setConnecting(true);
    poll.current = setInterval(async () => {
      if (popup && !popup.closed) return;
      if (poll.current) clearInterval(poll.current);
      await onRefreshIntegrations();
      setConnecting(false);
    }, 800);
  }
  return <div className="question-card email-card" aria-label={draft.title}>
    <h3 className="font-semibold">{draft.title}</h3>
    <div className="mt-4 space-y-3 text-sm">
      <div className="email-field"><span>From</span><span className="truncate">{from ?? "Gmail not connected"}</span></div>
      <label className="email-field"><span>To</span><input aria-label="To" value={to} disabled={locked} onChange={e => setTo(e.target.value)} /></label>
      <label className="email-field"><span>Cc</span><input aria-label="Cc" value={cc} disabled={locked} onChange={e => setCc(e.target.value)} /></label>
      <label className="email-field"><span>Subject</span><input aria-label="Subject" value={subject} disabled={locked} onChange={e => setSubject(e.target.value)} /></label>
    </div>
    <textarea aria-label="Email body" className="letter-editor" value={body} disabled={locked} onChange={e => setBody(e.target.value)} />
    {!!draft.attachments?.length && <div className="mt-3 flex flex-wrap gap-2">{draft.attachments.map(a => <span key={a.file_id} className="attachment-tag">{/\.(jpe?g|png|webp|heic|heif)$/i.test(a.name) ? <ImageIcon className="size-4" /> : <FileText className="size-4" />}{a.name}</span>)}</div>}
    {!!draft.sources?.length && <div className="mt-3"><SourceChips sources={draft.sources} /></div>}
    {status?.status === "failed" && <p className="mt-4 text-sm text-negative-deep">{status.message ?? "Gmail couldn't send this email."}</p>}
    <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
      {sending ? <span className="mr-auto inline-flex items-center gap-2 text-sm text-body"><Loader2 className="size-4 animate-spin" />Sending from Gmail…</span> : null}
      <Button variant="ghost" className="action-button" disabled={locked} onClick={onCancel}>Cancel</Button>
      {!from && !integrations?.gmail.connect_url ? <Button className="action-button" onClick={copy}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy letter"}</Button>
        : !from ? <Button className="action-button" disabled={locked || connecting} onClick={connect}>{connecting ? <Loader2 className="animate-spin" /> : <Mail />}Connect Gmail</Button>
        : <Button className="action-button" disabled={locked || !splitList(to).length || !subject.trim()} onClick={() => onSend(edit())}>{status?.status === "failed" ? <RotateCcw /> : <Mail />}{status?.status === "failed" ? "Retry" : "Send from Gmail"}</Button>}
    </div>
  </div>;
}
