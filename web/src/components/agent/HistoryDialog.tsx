import { useState } from "react";
import { History, Loader2, Paperclip, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { SessionSummary } from "@/agent/protocol";
import type { Agent } from "@/agent/useAgent";

const when = (seconds: number) => new Date(seconds * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

/** Header button and list of the signed-in user's saved chats: open one to continue it, or delete it. */
export function HistoryButton({ agent }: { agent: Agent }) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { listSessions, openSession, deleteSession } = agent;

  if (!agent.auth.user) return null;

  const show = async () => {  // the list is fetched each time it opens, so it is never stale
    setOpen(true);
    setSessions(null);
    setError(null);
    try { setSessions(await listSessions()); }
    catch (e) { setError(e instanceof Error ? e.message : "Your chats could not be loaded."); }
  };

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

  return <>
    <Button variant="ghost" onClick={() => void show()}><History />History</Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-h-[80vh] overflow-hidden rounded-3xl bg-card">
        <DialogTitle>Your chats</DialogTitle>
        <DialogDescription>Saved to your account. Only you can see them.</DialogDescription>
        {error && <p role="alert" className="text-sm text-negative-deep">{error}</p>}
        <div className="-mx-2 max-h-[60vh] overflow-y-auto">
          {sessions === null && !error && <p className="flex items-center gap-2 px-2 py-3 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Loading…</p>}
          {sessions?.length === 0 && <p className="px-2 py-3 text-sm text-muted-foreground">No saved chats yet. Chats you have while signed in show up here.</p>}
          {sessions?.map(s => <div key={s.session_id} className={`group flex items-center gap-2 rounded-xl px-2 hover:bg-secondary ${s.session_id === agent.state.sessionId ? "bg-secondary" : ""}`}>
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
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
