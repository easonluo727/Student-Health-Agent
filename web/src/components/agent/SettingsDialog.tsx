import { useState, type ReactNode } from "react";
import { Loader2, Settings } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { Agent } from "@/agent/useAgent";

interface Draft { name: string; zip: string; age: string; plan_id: string }

const field = "mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2 text-sm font-normal focus:outline-none focus:ring-2 focus:ring-ring";

function Row({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return <label className="block text-sm font-semibold">{label}{children}{help && <span className="mt-1 block text-xs font-normal text-muted-foreground">{help}</span>}</label>;
}

/** Header button and form for the signed-in user's name and the basics the agent remembers (memory.py). */
export function SettingsButton({ agent }: { agent: Agent }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const { getSettings, saveSettings } = agent;

  if (!agent.auth.user) return null;

  const show = async () => {
    setOpen(true);
    setDraft(null);
    setError(null);
    setSaved(false);
    try {
      const s = await getSettings();
      setDraft({ name: s.user.name ?? "", zip: s.settings.zip ?? "", age: s.settings.age == null ? "" : String(s.settings.age),
        plan_id: s.settings.plan_id ?? "" });
    } catch (e) { setError(e instanceof Error ? e.message : "Your settings could not be loaded."); }
  };
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => { setSaved(false); setDraft(d => d && { ...d, [k]: e.target.value }); };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    setSaving(true);
    setError(null);
    try {
      // An empty field clears it; the server checks every value.
      await saveSettings({ name: draft.name.trim() || null, zip: draft.zip.trim() || null, age: draft.age.trim() ? Number(draft.age) : null,
        plan_id: draft.plan_id || null });
      setSaved(true);
    } catch (err) {
      setError((err instanceof Error ? err.message : "Your settings could not be saved.").replace(/^Request failed \(\d+\): /, ""));
    } finally { setSaving(false); }
  };

  return <>
    <Button variant="ghost" size="icon" aria-label="Settings" title="Settings" onClick={() => void show()}><Settings /></Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="rounded-3xl bg-card">
        <DialogTitle>Settings</DialogTitle>
        <DialogDescription>The agent remembers these so it doesn&apos;t have to ask each time. Leave a field empty if you&apos;d rather not say.</DialogDescription>
        {!draft && !error && <p className="flex items-center gap-2 py-3 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Loading…</p>}
        {draft && <form className="space-y-4" onSubmit={e => void save(e)}>
          <Row label="Name"><input className={field} value={draft.name} onChange={set("name")} maxLength={80} autoComplete="name" placeholder="Your Google name" /></Row>
          <div className="grid grid-cols-2 gap-3">
            <Row label="ZIP code" help="For premiums and nearby doctors."><input className={field} value={draft.zip} onChange={set("zip")} inputMode="numeric" maxLength={5} autoComplete="postal-code" /></Row>
            <Row label="Age" help="Premiums depend on age."><input className={field} value={draft.age} onChange={set("age")} type="number" min={18} max={64} /></Row>
          </div>
          <Row label="Current plan"><select className={field} value={draft.plan_id} onChange={set("plan_id")}>
            <option value="">Not set</option>{agent.plans.map(p => <option key={p.plan_id} value={p.plan_id}>{p.name}{p.uploaded && !p.verified ? " (uploaded)" : ""}</option>)}
          </select></Row>
          {error && <p role="alert" className="text-sm text-negative-deep">{error}</p>}
          <div className="flex items-center justify-end gap-3">
            {saved && <span role="status" className="text-sm text-muted-foreground">Saved</span>}
            <Button type="submit" className="action-button" disabled={saving}>{saving && <Loader2 className="animate-spin" />}Save</Button>
          </div>
        </form>}
        {!draft && error && <p role="alert" className="text-sm text-negative-deep">{error}</p>}
      </DialogContent>
    </Dialog>
  </>;
}
