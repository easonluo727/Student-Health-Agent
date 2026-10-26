import { useMemo, useState } from "react";
import { ArrowLeft, Check, ExternalLink, AlertCircle, Loader2, MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { CareSection, Estimate, Plan, Provider, Report, ReplyTarget, SearchStep } from "@/agent/protocol";
import { SourceChip, SourceChips } from "./SourceChip";
import { costText, initials, money } from "./format";

type Ask = (t: ReplyTarget) => void;

export function StatusBadge({ tone, children }: { tone: "positive" | "warning" | "negative"; children: string }) {
  return <span className={`status-badge status-${tone}`}>{children}</span>;
}

// ---------- Plan comparison ----------
const lineClass = ["chart-anthem", "chart-oscar", "chart-blue"];
const lineName = ["solid", "dashed", "dotted"];

export function ReportView({ report, plansCatalog, building, onAsk, onOpenSearch }: { report: Report; plansCatalog: Plan[]; building: boolean; onAsk: Ask; onOpenSearch?: (() => void) | undefined }) {
  const scen = report.scenarios;
  const [sel, setSel] = useState(Math.floor(scen.length / 2));
  const idx = Math.min(sel, Math.max(0, scen.length - 1));
  const current = scen[idx];
  const recId = report.recommendation?.plan_id;
  const nameOf = (id: string) => report.plans.find(p => p.plan_id === id)?.name ?? plansCatalog.find(p => p.plan_id === id)?.name ?? id;

  const chart = useMemo(() => {
    const values = scen.flatMap(s => Object.values(s.costs).map(c => c.mid));
    if (!values.length) return null;
    const max = Math.max(...values), min = Math.min(...values);
    const x = (i: number) => (scen.length === 1 ? 335 : 60 + (i * 550) / (scen.length - 1));
    const y = (v: number) => (max === min ? 130 : 200 - ((v - min) / (max - min)) * 140);
    return { max, min, x, y };
  }, [scen]);

  if (!report.plans.length && report.care) return <div className="canvas-content"><CareView care={report.care} onAsk={onAsk} /></div>;
  return <div className="canvas-content">
    {report.recommendation ? <h2>{report.recommendation.headline}</h2> : building ? <div className="h-7 w-3/4 rounded-md bg-background" aria-label="Building recommendation" /> : null}
    <div className="mt-6">{report.plans.map(p => {
      const rec = p.plan_id === recId;
      const cost = current?.costs[p.plan_id];
      return <div className={`plan-row ${rec ? "recommended-plan" : ""}`} key={p.plan_id}>
        <span className="insurer-mark">{initials(p.insurer)}</span>
        <div className="min-w-0 flex-1">
          <Button className="plan-name" variant="link" onClick={() => onAsk({ kind: "dashboard_item", ref: `plan.${p.plan_id}`, label: p.name })}>{p.name}</Button>
          <p className="mt-1 text-sm text-body">{rec && report.recommendation?.reasons.length ? report.recommendation.reasons.join(" · ") : `${p.insurer} · ${p.network}${p.annual_premium != null ? ` · premium ${money(p.annual_premium)} a year` : ""}`}</p>
          {p.source && <div className="mt-2"><SourceChip source={p.source} /></div>}
        </div>
        <div className="text-right">{cost ? <p className="text-2xl font-semibold">{costText(cost)}</p> : building ? <p className="text-2xl font-semibold text-muted-foreground">—</p> : null}{rec && <StatusBadge tone="positive">Best fit</StatusBadge>}</div>
      </div>;
    })}</div>

    {scen.length > 0 && chart ? <section className="mt-9"><h2>Yearly cost by scenario</h2>{report.ranking?.summary && <p className="mt-2 text-sm text-body">{report.ranking.summary}</p>}
      <div className="cost-chart"><svg viewBox="0 0 640 220" role="img" aria-label="Yearly cost by scenario for each plan">
        <g className="chart-grid">{[60, 130, 200].map(v => <line key={v} x1="54" x2="620" y1={v} y2={v} />)}</g>
        <g className="chart-label"><text x="0" y="64">{money(chart.max)}</text>{chart.min !== chart.max && <text x="0" y="204">{money(chart.min)}</text>}
          {scen.map((s, i) => <text key={s.key} x={chart.x(i) - 2} y="218">{s.label}</text>)}</g>
        {report.plans.map((p, pi) => <polyline key={p.plan_id} className={lineClass[pi % 3]} points={scen.map((s, i) => s.costs[p.plan_id] ? `${chart.x(i)},${chart.y(s.costs[p.plan_id]!.mid)}` : "").filter(Boolean).join(" ")} />)}
        <line className="chart-marker" x1={chart.x(idx)} x2={chart.x(idx)} y1="30" y2="200" />
      </svg></div>
      <div className="flex flex-wrap justify-between gap-3 text-xs text-body">{report.plans.map((p, i) => <span key={p.plan_id}>{p.name} — {lineName[i % 3]}</span>)}</div>
      {scen.length > 1 && <label className="mt-5 flex items-center gap-4 text-sm">You: {current?.description}<input aria-label="Scenario" className="flex-1 accent-primary" type="range" min="0" max={scen.length - 1} value={idx} onChange={e => setSel(Number(e.target.value))} /></label>}
    </section> : building ? <div className="mt-8"><h2>Yearly cost by scenario</h2><div className="mt-4 h-56 rounded-lg bg-background" aria-label="Building chart" /></div> : null}

    {report.comparison.length > 0 && <section className="mt-9"><h2>What you&apos;d pay, by situation</h2>
      <table className="data-table mt-4"><thead><tr><th>Situation</th>{report.plans.map(p => <th key={p.plan_id}>{p.name}</th>)}</tr></thead>
        <tbody>{report.comparison.map(r => <tr key={r.key} className={r.highlight ? "row-relevant" : ""}>
          <td><button type="button" className="ask-link" title="Ask about this row" onClick={() => onAsk({ kind: "dashboard_item", ref: r.key, label: r.label })}>{r.label}</button></td>
          {report.plans.map(p => { const c = r.cells[p.plan_id]; return <td key={p.plan_id} title={c?.detail ?? undefined}>{c?.text ?? "—"}</td>; })}
        </tr>)}</tbody></table>
      <div className="mt-3"><SourceChips sources={report.comparison.flatMap(r => Object.values(r.cells).map(c => c.source))} /></div>
      {report.note && <p className="mt-3 text-xs text-muted-foreground">{report.note}</p>}
    </section>}
    {!report.comparison.length && report.note && <p className="mt-6 text-xs text-muted-foreground">{report.note}</p>}

    {!!report.recommendation?.excluded.length && <section className="mt-8"><h2>Plans I left out</h2>
      {report.recommendation.excluded.map((e, i) => <p key={e.plan_id} className={`${i ? "mt-2" : "mt-3"} text-sm text-muted-foreground`}>{nameOf(e.plan_id)} — {e.reason}</p>)}</section>}
    {report.care && <section className="mt-10"><CareView care={report.care} onAsk={onAsk} /></section>}
    {onOpenSearch && <Button variant="ghost" className="step-summary mt-6" onClick={onOpenSearch}>View web search details</Button>}
  </div>;
}

// ---------- Care ----------
type Sort = "distance" | "price" | "rating";
export function CareView({ care, onAsk }: { care: CareSection; onAsk: Ask }) {
  const [sort, setSort] = useState<Sort>("distance");
  const [inNetwork, setInNetwork] = useState(false);
  const q = care.query;
  // Display-only ordering/filtering of server-provided providers.
  const providers = useMemo(() => {
    const key: Record<Sort, (p: Provider) => number> = {
      distance: p => p.distance_miles ?? Infinity, price: p => p.patient_cost?.mid ?? Infinity, rating: p => -(p.quality?.score ?? -1),
    };
    return care.providers.filter(p => !inNetwork || p.in_network === true).slice().sort((a, b) => key[sort](a) - key[sort](b));
  }, [care.providers, sort, inNetwork]);
  const order = new Map(care.providers.map((p, i) => [p.provider_id, i + 1]));
  const pts = [...care.providers.map(p => [p.lat, p.lng]), ...(q.location ? [[q.location.lat, q.location.lng]] : [])];
  const lats = pts.map(p => p[0]!), lngs = pts.map(p => p[1]!);
  const [minLat, maxLat, minLng, maxLng] = [Math.min(...lats), Math.max(...lats), Math.min(...lngs), Math.max(...lngs)];
  const pos = (lat: number, lng: number) => ({ left: `${10 + (maxLng === minLng ? 40 : ((lng - minLng) / (maxLng - minLng)) * 80)}%`, top: `${12 + (maxLat === minLat ? 38 : ((maxLat - lat) / (maxLat - minLat)) * 72)}%` });
  const ask = (ref: string, label: string) => onAsk({ kind: "dashboard_item", ref, label });

  return <>
    {care.note && <h2>{care.note}</h2>}
    <div className="mt-4 flex flex-wrap gap-2">
      <Button variant="ghost" className="source-chip" onClick={() => ask("care.query.service", q.service)}>{q.service}{q.code ? ` · ${q.code}` : ""}</Button>
      {q.location && <Button variant="ghost" className="source-chip" onClick={() => ask("care.query.location", q.location!.label)}>{q.location.label}{q.max_miles != null ? ` · within ${q.max_miles} mi` : ""}</Button>}
      {q.plan_label && <Button variant="ghost" className="source-chip" onClick={() => ask("care.query.plan", q.plan_label!)}>{q.plan_label}</Button>}
    </div>
    {care.price && <div className="mt-5 rounded-xl bg-background p-4 text-sm"><p className="font-semibold">{care.price.label}: {money(care.price.midpoint)}</p><p className="mt-1 text-body">Range {money(care.price.min)}–{money(care.price.max)}</p>
      {care.price.bins.length > 0 && <div className="mt-3 flex h-12 items-end gap-1" aria-label="Price distribution">{care.price.bins.map(b => { const top = Math.max(...care.price!.bins.map(x => x.count)) || 1; return <span key={b.price} title={`${money(b.price)} · ${b.count}`} className="flex-1 rounded-sm bg-primary" style={{ height: `${(b.count / top) * 100}%` }} />; })}</div>}
      {care.price.source && <div className="mt-3"><SourceChip source={care.price.source} /></div>}</div>}
    <div className="provider-map" aria-label={`Map of providers near ${q.location?.label ?? q.service}`}>
      {q.location && <span className="map-label" style={pos(q.location.lat, q.location.lng)}>{q.location.label}</span>}
      {care.providers.map(p => <span key={p.provider_id} className="map-marker" style={pos(p.lat, p.lng)}>
        <Button className={`map-pin ${p.recommended ? "map-pin-recommended" : ""}`} size="icon" title={p.name} onClick={() => ask(`care.provider.${p.provider_id}`, p.name)}>{order.get(p.provider_id)}</Button>
        <span className="map-price">{p.patient_cost ? costText(p.patient_cost) : "—"}</span>
      </span>)}
    </div>
    <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-2 text-body">Sort by<select className="canvas-select" aria-label="Sort providers" value={sort} onChange={e => setSort(e.target.value as Sort)}><option value="distance">Distance</option><option value="price">Price</option><option value="rating">Rating</option></select></label>
      <label className="flex items-center gap-2 text-body"><input type="checkbox" className="accent-primary" checked={inNetwork} onChange={e => setInNetwork(e.target.checked)} />In-network only</label>
    </div>
    <div>{providers.map(p => <div className="doctor-row" key={p.provider_id}>
      <span className="doctor-number">{order.get(p.provider_id)}</span>
      <div className="min-w-0 flex-1">
        <p className="doctor-name">{p.name}</p>
        <p className="text-sm text-body">{[p.specialty, p.distance_miles != null ? `${p.distance_miles} mi` : null].filter(Boolean).join(" · ")}</p>
        {p.reason && <p className="mt-2 text-sm text-body">{p.reason}</p>}
        <p className="mt-2 text-xs text-muted-foreground">{p.quality ? (p.quality.score == null ? `Quality ratings: ${p.quality.label || "No public rating"}` : p.quality.label) : "No public rating"}{p.network_tier ? ` · ${p.network_tier}` : ""}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2">{p.source && <SourceChip source={p.source} />}{p.quality?.source && <SourceChip source={p.quality.source} />}
          <Button variant="ghost" className="source-chip" onClick={() => ask(`care.provider.${p.provider_id}`, p.name)}><MessageSquare className="size-3.5" />Ask</Button></div>
      </div>
      <div className="text-right"><p className="text-2xl font-semibold">{costText(p.patient_cost)}</p>
        {p.recommended ? <StatusBadge tone="positive">Agent pick</StatusBadge> : p.in_network === false ? <StatusBadge tone="negative">Out of network</StatusBadge> : p.in_network == null || p.price_vs_typical === "higher" || (p.patient_cost?.low != null && p.patient_cost?.high != null) ? <StatusBadge tone="warning">{p.in_network == null ? "Network unknown" : "Estimate range"}</StatusBadge> : <StatusBadge tone="positive">In network</StatusBadge>}</div>
    </div>)}</div>
  </>;
}

// ---------- Estimate (bill review) ----------
const statusText: Record<Estimate["status"], string> = { estimated: "Estimated", needs_information: "Needs information", needs_review: "Needs review", not_covered: "Not covered", invalid_input: "Invalid input" };
export function EstimateView({ estimate }: { estimate: Estimate }) {
  return <div className="canvas-content">
    <dl className="bill-meta"><div><dt>Plan</dt><dd>{estimate.plan_name}</dd></div><div><dt>Status</dt><dd>{statusText[estimate.status]}</dd></div>
      {estimate.patient_cost && <div><dt>You should owe</dt><dd>{costText(estimate.patient_cost)}</dd></div>}</dl>
    {estimate.rows.length > 0 && <table className="data-table bill-table"><thead><tr><th>Line</th><th>Amount</th></tr></thead><tbody>
      {estimate.rows.map(r => <tr key={r.label} className={r.amount == null ? "row-warning" : ""}>
        <td>{r.label}{r.note && <p className="mt-2 text-xs">{r.note}</p>}{r.amount == null && <div className="mt-2"><StatusBadge tone="warning">No data</StatusBadge></div>}</td>
        <td>{r.amount == null ? "No data" : money(r.amount)}</td>
      </tr>)}</tbody></table>}
    <div className="my-5"><SourceChips sources={estimate.rows.map(r => r.source)} /></div>
    {estimate.message && estimate.status !== "estimated" && <p className="estimate-message text-2xl font-semibold text-foreground">{estimate.message}</p>}
    {estimate.assumptions.map(a => <p key={a} className="mt-3 text-xs text-muted-foreground">{a}</p>)}
    {estimate.message && estimate.status === "estimated" && <p className="estimate-note mt-3 text-xs text-muted-foreground">{estimate.message}</p>}
  </div>;
}

// ---------- Web search / computer view ----------
export function SearchView({ steps, onBack }: { steps: SearchStep[]; onBack?: (() => void) | undefined }) {
  const query = steps.find(s => !s.url);
  const pages = steps.filter(s => s.url);
  const shots = steps.filter(s => s.screenshot);
  const [shot, setShot] = useState<string | null>(null);
  const current = shots.find(s => s.step_id === shot) ?? shots.at(-1);
  return <div className="canvas-content">
    {query && <><h2>Search query</h2><p className="mt-3 rounded-xl bg-background p-4">{query.text}</p></>}
    <h2 className="mt-8">Pages I visited</h2>
    {pages.map(s => <div className="search-row" key={s.step_id}><div className="min-w-0">
      <a className="source-title" href={s.url!} target="_blank" rel="noreferrer">{s.title ?? s.text}<ExternalLink /></a>
      <p className="mt-1 truncate text-xs text-muted-foreground">{s.url!.replace(/^https?:\/\/(www\.)?/, "")}</p>
      {s.title && <p className="mt-1 text-xs text-body">{s.text}</p>}
    </div><div className="flex items-center gap-2">{s.screenshot && <Button variant="ghost" className="source-chip" onClick={() => setShot(s.step_id)}>Screenshot</Button>}
      {s.status === "running" ? <Loader2 className="size-4 animate-spin" /> : s.status === "failed" ? <StatusBadge tone="warning">No data</StatusBadge> : <Check className="size-4 text-positive" />}</div></div>)}
    {!pages.length && <p className="mt-3 text-sm text-muted-foreground">No pages opened yet.</p>}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {current && <section className="mt-8" aria-label="Computer"><h2>Computer</h2><img className="computer-shot" src={current.screenshot!} alt={current.title ?? current.text} /><p className="mt-2 text-xs text-muted-foreground">{current.title ?? current.text}</p></section>}
    {steps.some(s => s.status === "failed") && !pages.length && <p className="mt-4 inline-flex items-center gap-2 text-sm text-body"><AlertCircle className="size-4" />Some steps failed.</p>}
    {onBack && <Button variant="secondary" className="action-button mt-8" onClick={onBack}><ArrowLeft />Back to the plan comparison</Button>}
  </div>;
}
