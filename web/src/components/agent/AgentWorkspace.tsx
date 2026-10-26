import { useRef, useState, type ReactNode } from "react";
import { ChevronRight, FileText, Image as ImageIcon, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ReplyTarget, Source } from "@/agent/protocol";
import type { Agent } from "@/agent/useAgent";
import { ConfirmCard, EmailCard, FormCard, SearchSteps } from "./Cards";
import { EstimateView, ReportView, SearchView } from "./Canvas";
import { Composer } from "./Composer";
import { Markdown } from "./Markdown";
import { SourceContext, SourceDialog } from "./SourceChip";

const prompts = ["What will a specialist visit cost me?", "Which 2027 plan fits me best?", "Check this bill for overcharges."];

/** The full agent UI. Framework-neutral: pass an agent from useAgent(transport). */
export function AgentWorkspace({ agent, toolbar }: { agent: Agent; toolbar?: ReactNode }) {
  const { state } = agent;
  const [source, setSource] = useState<Source | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const empty = state.items.length === 0;

  const searches = state.items.filter(i => i.kind === "search");
  const views: { id: string; title: string }[] = [
    ...(state.report ? [{ id: "report", title: state.report.title }] : []),
    ...Object.values(state.estimates).map(e => ({ id: `estimate:${e.estimate_id}`, title: e.title })),
    ...searches.filter(s => s.steps.some(x => x.kind === "web")).map(s => ({ id: `search:${s.searchId}`, title: `Web search · ${s.steps[0]?.title ?? s.steps[0]?.text ?? ""}` })),
  ];
  const view = views.find(v => v.id === state.canvasView) ?? views[0];
  const canvasOpen = state.canvasOpen && !!view;
  const building = state.busy && !!state.report && state.report.scenarios.length === 0 && state.report.plans.length > 0;
  const webSearch = searches.find(s => s.steps.some(x => x.kind === "web"));
  const ask = (t: ReplyTarget) => { agent.setReplyTarget(t); input.current?.focus(); };

  let canvas: ReactNode = null;
  if (view?.id === "report" && state.report) canvas = <ReportView key={state.report.report_id} report={state.report} plansCatalog={agent.plans} building={building} onAsk={ask}
    onOpenSearch={webSearch && !building ? () => agent.setCanvas(true, `search:${webSearch.searchId}`) : undefined} />;
  else if (view?.id.startsWith("estimate:")) { const e = state.estimates[view.id.slice(9)]; if (e) canvas = <EstimateView estimate={e} />; }
  else if (view?.id.startsWith("search:")) { const s = searches.find(x => `search:${x.searchId}` === view.id); if (s) canvas = <SearchView steps={s.steps} onBack={state.report ? () => agent.setCanvas(true, "report") : undefined} />; }

  return <SourceContext.Provider value={setSource}>
    <div className="insurance-workspace">
      <header className="workspace-header"><span className="text-sm font-semibold">Health insurance agent</span><Button variant="ghost" onClick={agent.newChat}>New chat</Button></header>
      {toolbar}
      <div className={`workspace-body ${canvasOpen ? "with-canvas" : "chat-only"}`}>
        <section className={`conversation ${empty ? "empty-conversation" : ""}`} aria-label="Conversation">
          <div className="conversation-scroll">
            {empty ? <h1 className="empty-title">Ask what care will cost, which plan fits you, or drop in a bill.</h1> : state.items.map(item => {
              switch (item.kind) {
                case "user": return <div className="user-message" key={item.id}>
                  {item.replyTarget && <p className="mb-2 text-xs text-muted-foreground">About: {item.replyTarget.label}</p>}
                  {item.text}
                  {item.attachments.length > 0 && <div className={`${item.text ? "mt-3" : ""} flex flex-wrap gap-2`}>{item.attachments.map(a => <span className="attachment-tag" key={a.file_id}>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    {a.preview ? <img src={a.preview} alt="" className="attachment-thumb" /> : a.content_type.startsWith("image/") ? <ImageIcon className="size-4" /> : <FileText className="size-4" />}{a.name}</span>)}</div>}
                </div>;
                case "agent": return <div className="agent-message" key={item.id}><Markdown text={item.text} />{item.streaming && <span className="stream-caret" aria-hidden />}</div>;
                case "search": { const web = item.steps.some(s => s.kind === "web"); return <SearchSteps key={item.id} steps={item.steps} onOpen={web ? () => agent.setCanvas(true, `search:${item.searchId}`) : undefined} />; }
                case "form": return <FormCard key={item.id} form={item.form} state={item.state} summary={item.summary} disabled={state.busy}
                  onSubmit={(answers, summary) => agent.submitForm(item.form.request_id, answers, summary)} onDismiss={() => agent.dismissForm(item.form.request_id)} />;
                case "confirm": return <ConfirmCard key={item.id} confirm={item.confirm} state={item.state} correction={item.correction} disabled={state.busy}
                  onRespond={(ok, c) => agent.respondConfirm(item.confirm.request_id, ok, c)} />;
                case "email": return <EmailCard key={item.id} draft={item.draft} status={item.status} state={item.state} disabled={state.busy && item.status?.status !== "sending"}
                  integrations={agent.integrations} onRefreshIntegrations={agent.refreshIntegrations}
                  onSend={d => agent.sendEmail(item.draft.request_id, d)} onCancel={() => agent.cancelEmail(item.draft.request_id)} />;
                case "estimate": return null;
                case "error": return <p key={item.id} role="alert" className="agent-message text-negative-deep">{item.message}</p>;
              }
            })}
            {!empty && !canvasOpen && views.length > 0 && <Button variant="secondary" className="action-button mt-5" onClick={() => agent.setCanvas(true)}>Open page</Button>}
          </div>
          <div className="composer-area">
            <Composer agent={agent} inputRef={input} />
            {empty && <div className="example-prompts">{prompts.map(p => <Button variant="ghost" key={p} onClick={() => agent.sendMessage(p)}>{p}<ChevronRight /></Button>)}</div>}
          </div>
        </section>
        {canvasOpen && view && <aside className="canvas-panel" aria-label="Canvas">
          <div className="canvas-header">
            <div className="min-w-0"><h1 className="truncate text-sm font-semibold">{view.title}</h1>{building && <p className="mt-1 text-xs text-muted-foreground">Building…</p>}</div>
            <div className="flex shrink-0 items-center gap-2">
              {views.length > 1 && <select className="canvas-select" aria-label="Switch canvas page" value={view.id} onChange={e => agent.setCanvas(true, e.target.value)}>{views.map(v => <option key={v.id} value={v.id}>{v.title}</option>)}</select>}
              <Button variant="ghost" size="icon" aria-label="Close canvas" title="Close canvas" onClick={() => agent.setCanvas(false)}><X /></Button>
            </div>
          </div>
          <div className="canvas-scroll">{canvas}</div>
        </aside>}
      </div>
      <SourceDialog source={source} onClose={() => setSource(null)} />
    </div>
  </SourceContext.Provider>;
}
