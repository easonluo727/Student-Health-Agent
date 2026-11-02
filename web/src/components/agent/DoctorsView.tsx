import { useCallback, useEffect, useRef, useState } from "react";
import "leaflet/dist/leaflet.css";
import type { Map as LeafletMap, Marker } from "leaflet";
import { ChevronDown, ChevronRight, MessageSquare, Navigation, Phone } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { DoctorPrice, DoctorsPage, PageDoctor, ReplyTarget } from "@/agent/protocol";
import { money } from "./format";

// Display only: every amount comes from the page (Python), and a missing price says so instead of showing $0.
function priceText(p: DoctorPrice | undefined): { text: string; muted: boolean } {
  if (!p) return { text: "No data", muted: true };
  if (p.patient_cost != null) return { text: money(p.patient_cost), muted: false };
  if (p.status === "partial_rate_only" && p.negotiated_rate != null) return { text: `Rate ${money(p.negotiated_rate)}, your share unknown`, muted: true };
  if (p.status === "needs_information") return { text: "Needs your remaining deductible", muted: true };
  if (p.status === "not_covered") return { text: "Not covered", muted: true };
  return { text: "No price data", muted: true };
}

const phoneText = (p: string) => p.replace(/^(\d{3})(\d{3})(\d{4})$/, "($1) $2-$3");
const miles = (d: number | null) => d == null ? null : `${d < 10 ? d.toFixed(1) : Math.round(d)} mi`;

/** Leaflet map: the user, the agent's picks (numbered, mint) and everyone else (small grey). */
function DoctorsMap({ page, active, onSelect }: { page: DoctorsPage; active: string | null; onSelect: (npi: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<LeafletMap | null>(null);
  const markers = useRef<Map<string, Marker>>(new Map());

  useEffect(() => {
    let live = true;
    void import("leaflet").then(L => {  // Leaflet needs window, so it loads in the browser only
      if (!live || !box.current) return;
      const m = L.map(box.current, { scrollWheelZoom: false, zoomControl: true, attributionControl: true });
      map.current = m;
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(m);
      const points: [number, number][] = [];
      if (page.user_position) {
        const at: [number, number] = [page.user_position.lat, page.user_position.lng];
        L.marker(at, { icon: L.divIcon({ className: "", html: '<span class="map-you"></span>', iconSize: [18, 18], iconAnchor: [9, 9] }), title: page.user_position.label, zIndexOffset: 1000 })
          .bindTooltip(`You · ${page.user_position.label}`).addTo(m);
        points.push(at);
      }
      // Doctors in the same building share coordinates: draw them in a small ring (in pixels, so it holds at every
      // zoom); each marker keeps its real position.
      const seen = new Map<string, number>();
      const key = (d: PageDoctor) => `${d.lat!.toFixed(5)},${d.lng!.toFixed(5)}`;
      const ring = (d: PageDoctor): [number, number] => {
        const crowd = page.doctors.filter(x => x.lat != null && x.lng != null && key(x) === key(d)).length;
        const i = seen.get(key(d)) ?? 0;
        seen.set(key(d), i + 1);
        if (crowd < 2) return [0, 0];
        const angle = (2 * Math.PI * i) / crowd - Math.PI / 2, r = crowd > 6 ? 30 : 22;
        return [Math.round(r * Math.cos(angle)), Math.round(r * Math.sin(angle))];
      };
      for (const d of [...page.doctors].reverse()) {  // picks drawn last, on top
        if (d.lat == null || d.lng == null) continue;
        const [dx, dy] = ring(d);
        const html = d.pick ? `<span class="map-pick">${d.pick}</span>` : '<span class="map-other"></span>';
        const size = d.pick ? 28 : 12;
        const marker = L.marker([d.lat, d.lng], { icon: L.divIcon({ className: "", html, iconSize: [size, size], iconAnchor: [size / 2 - dx, size / 2 - dy] }), title: d.name, zIndexOffset: d.pick ? 500 - d.pick : 0 })
          .bindTooltip(`${d.name}${d.distance_miles != null ? ` · ${miles(d.distance_miles)}` : ""}`)
          .on("click", () => onSelect(d.npi))
          .addTo(m);
        markers.current.set(d.npi, marker);
        if (d.pick || page.doctors.every(x => !x.pick)) points.push([d.lat, d.lng]);
      }
      if (points.length > 1) m.fitBounds(L.latLngBounds(points), { padding: [36, 36], maxZoom: 15 });
      else if (points.length === 1) m.setView(points[0]!, 14);
      else m.setView([34.05, -118.25], 11);
    });
    const markerMap = markers.current;
    return () => { live = false; map.current?.remove(); map.current = null; markerMap.clear(); };
  }, [page, onSelect]);

  useEffect(() => {
    const marker = active ? markers.current.get(active) : undefined;
    if (marker && map.current) { map.current.panTo(marker.getLatLng()); marker.openTooltip(); }
  }, [active]);

  return <div ref={box} className="doctors-map" role="region" aria-label="Map of the doctors" />;
}

function Prices({ page, doctor }: { page: DoctorsPage; doctor: PageDoctor }) {
  return <dl className="mt-3 grid gap-1">
    {page.plans.map(plan => {
      const p = priceText(doctor.prices[plan.plan_id]);
      return <div key={plan.plan_id} className="flex items-baseline justify-between gap-4 text-sm">
        <dt className="text-muted-foreground">{page.plans.length > 1 ? plan.name : "You pay"}</dt>
        <dd className={p.muted ? "text-muted-foreground" : "text-lg font-semibold tabular-nums"}>{p.text}</dd>
      </div>;
    })}
  </dl>;
}

/** The doctor page: map, the agent's picks with its reasons, then everyone else nearest first. */
export function DoctorsView({ page, onAsk }: { page: DoctorsPage; onAsk?: ((t: ReplyTarget) => void) | undefined }) {
  const [active, setActive] = useState<string | null>(null);
  const [showRest, setShowRest] = useState(false);
  const cards = useRef<Map<string, HTMLElement>>(new Map());
  const picks = page.doctors.filter(d => d.pick);
  const rest = page.doctors.filter(d => !d.pick);
  const shown = picks.length ? picks : rest.slice(0, 5);  // no picks: the nearest five as cards
  const others = picks.length ? rest : rest.slice(5);

  const select = useCallback((npi: string) => {  // a marker was clicked: bring its card or row into view
    setActive(npi);
    const card = cards.current.get(npi);
    if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    else setShowRest(true);
  }, []);

  return <div className="doctors-page">
    <DoctorsMap page={page} active={active} onSelect={select} />
    <section aria-label={picks.length ? "Recommended doctors" : "Nearest doctors"} className="mt-5">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{picks.length ? "Recommended" : "Nearest"}</h2>
      <div className="grid gap-3">
        {shown.map(d => <article key={d.npi} ref={el => { if (el) cards.current.set(d.npi, el); else cards.current.delete(d.npi); }}
          className={`doctor-card ${active === d.npi ? "active" : ""}`} onMouseEnter={() => setActive(d.npi)}>
          <div className="flex items-start gap-3">
            {d.pick && <span className="doctor-rank" aria-label={`Pick ${d.pick}`}>{d.pick}</span>}
            <div className="min-w-0 flex-1">
              <h3 className="truncate font-semibold">{d.name}</h3>
              <p className="text-sm text-muted-foreground">{[d.specialty, miles(d.distance_miles) && `${miles(d.distance_miles)} away`].filter(Boolean).join(" · ")}</p>
            </div>
          </div>
          {d.note && <p className="mt-3 text-sm">{d.note}</p>}
          <Prices page={page} doctor={d} />
          <p className="mt-3 text-xs text-muted-foreground">{d.address}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {d.phone && <Button asChild variant="secondary" size="sm"><a href={`tel:${d.phone}`}><Phone />{phoneText(d.phone)}</a></Button>}
            {d.directions_url && <Button asChild variant="secondary" size="sm"><a href={d.directions_url} target="_blank" rel="noreferrer"><Navigation />Directions</a></Button>}
            {onAsk && <Button variant="ghost" size="sm" onClick={() => onAsk({ kind: "dashboard_item", ref: `doctor:${d.npi}`, label: d.name })}><MessageSquare />Ask about</Button>}
          </div>
        </article>)}
      </div>
    </section>
    {others.length > 0 && <section className="mt-5">
      <Button variant="ghost" className="-ml-3" aria-expanded={showRest} onClick={() => setShowRest(v => !v)}>
        {showRest ? <ChevronDown /> : <ChevronRight />}{others.length} more nearby</Button>
      {showRest && <table className="doctors-table mt-2 w-full text-sm">
        <thead><tr><th>Doctor</th><th>Distance</th>{page.plans.map(p => <th key={p.plan_id}>{page.plans.length > 1 ? p.name : "You pay"}</th>)}</tr></thead>
        <tbody>{others.map(d => <tr key={d.npi} className={active === d.npi ? "active" : ""} onMouseEnter={() => setActive(d.npi)}>
          <td>{d.name}</td><td className="tabular-nums">{miles(d.distance_miles) ?? "—"}</td>
          {page.plans.map(p => { const t = priceText(d.prices[p.plan_id]); return <td key={p.plan_id} className={t.muted ? "text-muted-foreground" : "tabular-nums"}>{t.text}</td>; })}
        </tr>)}</tbody>
      </table>}
    </section>}
    <p className="mt-6 text-xs text-muted-foreground">{page.note}</p>
  </div>;
}
