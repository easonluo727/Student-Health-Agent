import { createContext, useContext } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { Source } from "@/agent/protocol";

export const SourceContext = createContext<(s: Source) => void>(() => {});

const kindLabel: Record<Source["kind"], string> = {
  policy: "Plan document", mrf: "Insurer price file", web: "Web page", user_form: "Your answers", user_message: "Your message", inferred: "Inferred by the agent",
};

export function SourceChip({ source }: { source: Source }) {
  const open = useContext(SourceContext);
  return <Button variant="ghost" className="source-chip" onClick={() => open(source)}>{source.label}</Button>;
}

export function SourceChips({ sources }: { sources: (Source | null | undefined)[] }) {
  const seen = new Set<string>();
  const list = sources.filter((s): s is Source => !!s && !seen.has(s.label) && !!seen.add(s.label));
  if (!list.length) return null;
  return <div className="flex flex-wrap gap-2">{list.map(s => <SourceChip key={s.label} source={s} />)}</div>;
}

export function SourceDialog({ source, onClose }: { source: Source | null; onClose: () => void }) {
  return <Dialog open={!!source} onOpenChange={o => { if (!o) onClose(); }}>
    <DialogContent className="rounded-3xl bg-card">
      <DialogTitle className="pr-6">{source?.label}</DialogTitle>
      <DialogDescription>{source ? kindLabel[source.kind] : ""}{source?.page != null ? ` · page ${source.page}` : ""}</DialogDescription>
      {source?.quote && <blockquote className="border-l-2 border-foreground bg-accent p-4 text-sm leading-6">“{source.quote}”</blockquote>}
      {source?.url && <a href={source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm underline underline-offset-2">{source.url}<ExternalLink className="size-3.5" /></a>}
    </DialogContent>
  </Dialog>;
}
