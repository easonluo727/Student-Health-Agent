import { useState, type ReactNode } from "react";
import { ExternalLink, FileText, History, Image as ImageIcon, Loader2, MessageSquare, Paperclip, Receipt, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { BillReview, SessionSummary, StoredUpload } from "@/agent/protocol";
import type { Agent } from "@/agent/useAgent";
import { money } from "./format";

const when = (seconds: number) => new Date(seconds * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const day = (seconds: number) => new Date(seconds * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

type Tab = "chats" | "files";

function Muted({ children }: { children: ReactNode }) {
  return <p className="flex items-center gap-2 px-2 py-3 text-sm text-muted-foreground">{children}</p>;
}

/** One bill review in a line: the numbers come from the audit (Python), shown as they are. */
function ReviewLine({ review }: { review: BillReview }) {
  if (review.status !== "audited") return <span>Couldn&apos;t be checked against negotiated rates</span>;
  return <span>
    {review.billed != null && <>Billed {money(review.billed)} · </>}
    {review.you_owe != null && <>you owe {money(review.you_owe)}</>}
    {review.possible_overcharge ? <span className="font-semibold text-negative-deep"> · {money(review.possible_overcharge)} possibly overcharged</span> : <span className="text-positive-deep"> · no overcharge found</span>}
    {review.appeal_drafted && <> · appeal drafted</>}
  </span>;
}

/** Header button: the signed-in user's saved chats (open to continue, or delete) and the files they uploaded. */
export function HistoryButton({ agent }: { agent: Agent }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("chats");
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [uploads, setUploads] = useState<StoredUpload[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { listSessions, listUploads, openSession, deleteSession, fileUrl } = agent;

  if (!agent.auth.user) return null;

  // Each list is fetched when it is shown, so it is never stale.
  const load = async (which: Tab) => {
    setTab(which);
    setError(null);
    try {
      if (which === "chats") { setSessions(null); setSessions(await listSessions()); }
      else { setUploads(null); setUploads(await listUploads()); }
    } catch (e) { setError(e instanceof Error ? e.message : "This list could not be loaded."); }
  };
  const show = () => { setOpen(true); void load("chats"); };

  const reopen = async (id: string) => {
    setBusyId(id);
    setError(null);
    try { await openSession(id); setOpen(false); }
    catch (e) { setError(e instanceof Error ? e.message : "This chat could not be opened."); }
    finally { setBusyId(null); }
  };
  const remove = async (s: SessionSummary) => {
    if (!window.confirm(`Delete "${s.title ?? "Untitled chat"}" and its files? This can't be undone.`)) return;
    setBusyId(s.session_id);
    setError(null);
    try { await deleteSession(s.session_id); setSessions(list => list?.filter(x => x.session_id !== s.session_id) ?? null); }
    catch (e) { setError(e instanceof Error ? e.message : "This chat could not be deleted."); }
    finally { setBusyId(null); }
  };

  const planOf = (planId: string | null) => agent.plans.find(p => p.plan_id === planId);  // the plan database is the authority
  const fileActions = (u: StoredUpload) => <div className="mt-2 flex flex-wrap gap-2">
    {fileUrl(u.file_id) && <Button asChild variant="secondary" size="sm"><a href={fileUrl(u.file_id)!} target="_blank" rel="noreferrer"><ExternalLink />Open file</a></Button>}
    <Button variant="secondary" size="sm" disabled={busyId !== null} onClick={() => void reopen(u.session_id)}>
      {busyId === u.session_id ? <Loader2 className="animate-spin" /> : <MessageSquare />}Open chat</Button>
  </div>;
  const section = (title: string, list: StoredUpload[], row: (u: StoredUpload) => ReactNode) => list.length > 0 && <section className="mb-4">
    <h2 className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h2>
    {list.map(u => <div key={u.file_id} className="rounded-xl px-2 py-3 hover:bg-secondary">{row(u)}{fileActions(u)}</div>)}
  </section>;

  const bills = uploads?.filter(u => u.kind === "bill") ?? [];
  const plans = uploads?.filter(u => u.kind === "plan") ?? [];
  const other = uploads?.filter(u => !u.kind) ?? [];

  return <>
    <Button variant="ghost" onClick={show}><History />History</Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-h-[80vh] overflow-hidden rounded-3xl bg-card">
        <DialogTitle>Your history</DialogTitle>
        <DialogDescription>Saved to your account. Only you can see it.</DialogDescription>
        <div role="tablist" className="flex gap-1 rounded-full bg-secondary p-1 text-sm">
          {(["chats", "files"] as const).map(t => <button key={t} type="button" role="tab" aria-selected={tab === t}
            className={`flex-1 rounded-full px-3 py-1.5 font-semibold ${tab === t ? "bg-card shadow-sm" : "text-muted-foreground"}`}
            onClick={() => void load(t)}>{t === "chats" ? "Chats" : "Bills & plans"}</button>)}
        </div>
        {error && <p role="alert" className="text-sm text-negative-deep">{error}</p>}
        <div className="-mx-2 max-h-[55vh] overflow-y-auto" role="tabpanel">
          {tab === "chats" && <>
            {sessions === null && !error && <Muted><Loader2 className="size-4 animate-spin" />Loading…</Muted>}
            {sessions?.length === 0 && <Muted>No saved chats yet. Chats you have while signed in show up here.</Muted>}
            {sessions?.map(s => <div key={s.session_id} className={`flex items-center gap-2 rounded-xl px-2 hover:bg-secondary ${s.session_id === agent.state.sessionId ? "bg-secondary" : ""}`}>
              <button type="button" className="min-w-0 flex-1 py-3 text-left disabled:opacity-60" disabled={busyId !== null} onClick={() => void reopen(s.session_id)}>
                <span className="block truncate text-sm font-semibold">{s.title ?? "Untitled chat"}</span>
                <span className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                  {when(s.updated_at)}
                  {s.uploads > 0 && <span className="flex items-center gap-1"><Paperclip className="size-3" />{s.uploads} {s.uploads === 1 ? "file" : "files"}</span>}
                </span>
              </button>
              {busyId === s.session_id
                ? <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" aria-label="Working" />
                : <Button variant="ghost" size="icon" aria-label={`Delete ${s.title ?? "chat"}`} title="Delete" disabled={busyId !== null} onClick={() => void remove(s)}><Trash2 /></Button>}
            </div>)}
          </>}
          {tab === "files" && <>
            {uploads === null && !error && <Muted><Loader2 className="size-4 animate-spin" />Loading…</Muted>}
            {uploads?.length === 0 && <Muted>No files yet. Bills and plan PDFs you upload while signed in show up here.</Muted>}
            {section("Bills", bills, u => <>
              <p className="flex items-center gap-2 text-sm font-semibold"><Receipt className="size-4 shrink-0" />
                <span className="truncate">{u.bill_reviews[0]?.provider ?? u.name}</span></p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {u.bill_reviews[0]?.statement_date ? `Statement ${u.bill_reviews[0].statement_date}` : `Uploaded ${day(u.created_at)}`}
                {u.bill_reviews[0]?.plan_name ? ` · ${u.bill_reviews[0].plan_name}` : ""}</p>
              {u.bill_reviews.length === 0 && <p className="mt-1 text-sm text-muted-foreground">Read, not reviewed yet</p>}
              {u.bill_reviews.map(r => <p key={r.bill_id} className="mt-1 text-sm"><ReviewLine review={r} /></p>)}
            </>)}
            {section("Plans", plans, u => <>
              <p className="flex items-center gap-2 text-sm font-semibold"><FileText className="size-4 shrink-0" />
                <span className="truncate">{planOf(u.plan_id)?.name ?? u.plan_name ?? u.name}</span></p>
              <p className="mt-0.5 text-xs text-muted-foreground">{u.name} · uploaded {day(u.created_at)}
                {planOf(u.plan_id)?.verified === false && <> · <span className="text-warning-content">extracted automatically, not checked by a person</span></>}</p>
            </>)}
            {section("Other files", other, u => <>
              <p className="flex items-center gap-2 text-sm font-semibold">{u.content_type.startsWith("image/") ? <ImageIcon className="size-4 shrink-0" /> : <FileText className="size-4 shrink-0" />}
                <span className="truncate">{u.name}</span></p>
              <p className="mt-0.5 text-xs text-muted-foreground">Uploaded {day(u.created_at)} · not read yet</p>
            </>)}
          </>}
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
