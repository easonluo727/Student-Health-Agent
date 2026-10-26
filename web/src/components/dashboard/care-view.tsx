"use client"

// The care dashboard: what the agent searched for, the price spread, and specific providers on a list and a map.
// Every number comes from the report (Python); filters and sort only change what is shown and in which order. The
// default order is the backend's (its recommendation first). The query bar is read-only: clicking a field asks the
// agent to change it.

import dynamic from "next/dynamic"
import { useMemo, useState } from "react"
import {
  ArrowDownRight,
  ArrowUpRight,
  BadgeCheck,
  ChevronDown,
  CircleHelp,
  MapPin,
  MessageSquareText,
  Search,
  ShieldCheck,
  ShieldQuestion,
  ShieldX,
  Sparkles,
  Star,
} from "lucide-react"
import type { CareSection, Provider, ProviderKind, ReplyTarget } from "@/lib/protocol"
import { SourceChip, costText, usd } from "@/components/chat/source-chip"
import { cn } from "@/lib/utils"
import { PriceChart } from "./price-chart"

const CareMap = dynamic(() => import("./care-map"), {
  ssr: false,
  loading: () => <div className="size-full animate-pulse bg-muted" />,
})

const KIND_LABEL: Record<ProviderKind, string> = {
  doctor: "Doctor",
  hospital: "Hospital",
  clinic: "Clinic",
  imaging_center: "Imaging center",
  lab: "Lab",
  urgent_care: "Urgent care",
  other: "Provider",
}

type Sort = "recommended" | "price" | "distance" | "quality"
const SORT_LABEL: Record<Sort, string> = {
  recommended: "Recommended",
  price: "Lowest price",
  distance: "Nearest",
  quality: "Best rated",
}
const DISTANCES = [null, 5, 10, 25, 50] as const

const cost = (p: Provider) => p.patient_cost?.mid ?? p.allowed_amount ?? null

export function CareView({ care, onAsk }: { care: CareSection; onAsk: (target: ReplyTarget) => void }) {
  const { query, price, providers } = care
  const [miles, setMiles] = useState<number | null>(query.max_miles ?? null)
  const [inNetworkOnly, setInNetworkOnly] = useState(false)
  const [minQuality, setMinQuality] = useState<number | null>(null)
  const [sort, setSort] = useState<Sort>("recommended")
  const [activeId, setActiveId] = useState<string | null>(null)

  const shown = useMemo(() => {
    const kept = providers.filter(
      (p) =>
        (miles == null || p.distance_miles == null || p.distance_miles <= miles) &&
        (!inNetworkOnly || p.in_network === true) &&
        (minQuality == null || (p.quality?.score ?? 0) >= minQuality),
    )
    const last = Number.POSITIVE_INFINITY
    const by: Record<Sort, (p: Provider) => number> = {
      recommended: () => 0,
      price: (p) => cost(p) ?? last,
      distance: (p) => p.distance_miles ?? last,
      quality: (p) => -(p.quality?.score ?? -1),
    }
    return sort === "recommended" ? kept : [...kept].sort((a, b) => by[sort](a) - by[sort](b))
  }, [providers, miles, inNetworkOnly, minQuality, sort])

  const ask = (ref: string, label: string) => onAsk({ kind: "dashboard_item", ref, label })
  const scrollTo = (id: string) =>
    document.getElementById(`provider-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" })

  return (
    <div className="space-y-5 p-6">
      {/* Query bar: what the agent understood. Read-only; each field asks the agent. */}
      <div className="grid grid-cols-[1.3fr_1fr_1.2fr] divide-x rounded-2xl border bg-card shadow-sm">
        <QueryField
          icon={<Search className="size-4.5" />}
          label="Care"
          value={query.service}
          onClick={() => ask("care.query.service", `Care: ${query.service}`)}
        />
        <QueryField
          icon={<MapPin className="size-4.5" />}
          label="Location"
          value={query.location?.label ?? "Not set"}
          onClick={() => ask("care.query.location", `Location: ${query.location?.label ?? "not set"}`)}
        />
        <QueryField
          icon={<ShieldCheck className="size-4.5" />}
          label="Insurance"
          value={query.plan_label ?? "Not set"}
          onClick={() => ask("care.query.plan", `Insurance: ${query.plan_label ?? "not set"}`)}
        />
      </div>

      {/* Filters: display only */}
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={miles == null ? "any" : String(miles)}
          options={DISTANCES.map((d) => ({ value: d == null ? "any" : String(d), label: d == null ? "Any distance" : `Within ${d} miles` }))}
          onChange={(v) => setMiles(v === "any" ? null : Number(v))}
        />
        <Select
          value={minQuality == null ? "any" : String(minQuality)}
          options={[
            { value: "any", label: "Any rating" },
            { value: "3", label: "3+ stars" },
            { value: "4", label: "4+ stars" },
          ]}
          onChange={(v) => setMinQuality(v === "any" ? null : Number(v))}
        />
        <button
          onClick={() => setInNetworkOnly((v) => !v)}
          aria-pressed={inNetworkOnly}
          className={cn(
            "inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-sm transition-colors",
            inNetworkOnly ? "border-primary bg-accent text-accent-foreground" : "bg-card hover:bg-muted",
          )}
        >
          <ShieldCheck className="size-4" /> In network only
        </button>
        <div className="ml-auto">
          <Select
            value={sort}
            options={(Object.keys(SORT_LABEL) as Sort[]).map((s) => ({ value: s, label: SORT_LABEL[s] }))}
            onChange={(v) => setSort(v as Sort)}
          />
        </div>
      </div>

      {/* What it is + what it costs */}
      <div className={cn("grid gap-5", price ? "grid-cols-[1fr_1.15fr]" : "grid-cols-1")}>
        <div className="py-2">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-2xl font-semibold tracking-tight">{query.service}</h2>
            {query.code && (
              <span className="rounded-md border bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                {query.code}
              </span>
            )}
          </div>
          {query.description && (
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{query.description}</p>
          )}
          {care.note && (
            <p className="mt-3 inline-flex items-start gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs text-accent-foreground">
              <CircleHelp className="mt-0.5 size-3.5 shrink-0" />
              {care.note}
            </p>
          )}
        </div>
        {price && (
          <div className="grid grid-cols-[0.8fr_1.2fr] items-center gap-3 rounded-2xl border bg-card p-4 shadow-sm">
            <div className="text-center">
              <div className="text-3xl font-semibold tracking-tight tabular-nums">{usd(price.midpoint)}</div>
              <p className="mt-1 text-xs leading-snug text-muted-foreground">{price.label}</p>
              {price.source && (
                <div className="mt-2">
                  <SourceChip source={price.source} />
                </div>
              )}
            </div>
            <div className="h-36">
              <PriceChart price={price} />
            </div>
          </div>
        )}
      </div>

      {/* Providers: list + map */}
      <div className="grid grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] gap-5">
        <div className="min-w-0">
          <div className="mb-3 flex items-baseline justify-between">
            <h3 className="text-sm font-semibold">
              {shown.length} {shown.length === 1 ? "place" : "places"} for {query.service}
            </h3>
            {shown.length !== providers.length && (
              <span className="text-xs text-muted-foreground">{providers.length - shown.length} hidden by filters</span>
            )}
          </div>
          <div className="space-y-3">
            {shown.map((p) => (
              <ProviderCard
                key={p.provider_id}
                provider={p}
                active={p.provider_id === activeId}
                onHover={setActiveId}
                onAsk={() => ask(`care.provider.${p.provider_id}`, p.name)}
              />
            ))}
            {shown.length === 0 && (
              <div className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
                No places match these filters.
              </div>
            )}
          </div>
        </div>
        <div className="sticky top-4 h-[560px] self-start overflow-hidden rounded-2xl border shadow-sm">
          <CareMap
            query={query}
            providers={shown}
            activeId={activeId}
            onHover={setActiveId}
            onSelect={(id) => {
              setActiveId(id)
              scrollTo(id)
            }}
          />
        </div>
      </div>
    </div>
  )
}

function QueryField({
  icon,
  label,
  value,
  onClick,
}: {
  icon: React.ReactNode
  label: string
  value: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      title="Ask the agent to change this"
      className="group flex min-w-0 items-center gap-3 px-4 py-3 text-left transition-colors first:rounded-l-2xl last:rounded-r-2xl hover:bg-muted/60"
    >
      <span className="text-muted-foreground">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block text-[11px] text-muted-foreground">{label}</span>
        <span className="block truncate font-medium">{value}</span>
      </span>
      <MessageSquareText className="size-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-70" />
    </button>
  )
}

function Select({
  value,
  options,
  onChange,
}: {
  value: string
  options: { value: string; label: string }[]
  onChange: (value: string) => void
}) {
  return (
    <label className="relative inline-flex h-9 items-center rounded-lg border bg-card text-sm hover:bg-muted">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-full cursor-pointer appearance-none bg-transparent pr-8 pl-3 outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2.5 size-4 text-muted-foreground" />
    </label>
  )
}

function ProviderCard({
  provider: p,
  active,
  onHover,
  onAsk,
}: {
  provider: Provider
  active: boolean
  onHover: (id: string | null) => void
  onAsk: () => void
}) {
  const youPay = p.patient_cost
  return (
    <article
      id={`provider-${p.provider_id}`}
      onMouseEnter={() => onHover(p.provider_id)}
      onMouseLeave={() => onHover(null)}
      className={cn(
        "grid grid-cols-[1fr_auto] gap-4 rounded-2xl border bg-card p-4 transition-shadow",
        active ? "border-ring shadow-md" : "shadow-sm",
        p.recommended && "ring-1 ring-primary/30",
      )}
    >
      <div className="min-w-0">
        {p.recommended && (
          <span className="mb-1.5 inline-flex items-center gap-1 rounded-full bg-primary px-2 py-0.5 text-[11px] font-medium text-primary-foreground">
            <Sparkles className="size-3" /> Agent pick
          </span>
        )}
        <h4 className="truncate font-semibold">{p.name}</h4>
        <p className="text-xs text-muted-foreground">
          {KIND_LABEL[p.kind]}
          {p.specialty && ` · ${p.specialty}`}
        </p>
        <div className="mt-2.5 space-y-1 text-sm">
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <MapPin className="size-3.5 shrink-0" />
            <span className="truncate">
              {p.address}
              {p.distance_miles != null && ` (${p.distance_miles.toFixed(1)} mi)`}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <NetworkBadge provider={p} />
            <Quality provider={p} />
          </div>
        </div>
        {p.reason && <p className="mt-2 text-xs leading-relaxed text-foreground/80">{p.reason}</p>}
      </div>

      <div className="flex flex-col items-end justify-between gap-2 text-right">
        <div>
          {youPay ? (
            <>
              <div className="text-[11px] text-muted-foreground">You pay</div>
              <div className="text-xl font-semibold tracking-tight tabular-nums">{costText(youPay)}</div>
            </>
          ) : (
            <div className="text-sm text-muted-foreground">Price unknown</div>
          )}
          {p.allowed_amount != null && (
            <div className="text-[11px] text-muted-foreground tabular-nums">
              Negotiated rate {usd(p.allowed_amount)}
            </div>
          )}
          <VsTypical value={p.price_vs_typical} />
        </div>
        <div className="flex items-center gap-1.5">
          {p.source && <SourceChip source={p.source} />}
          <button
            onClick={onAsk}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <MessageSquareText className="size-3.5" /> Ask
          </button>
        </div>
      </div>
    </article>
  )
}

function NetworkBadge({ provider: p }: { provider: Provider }) {
  if (p.in_network === true)
    return (
      <span className="inline-flex items-center gap-1 text-xs text-emerald-700">
        <BadgeCheck className="size-3.5" /> In network{p.network_tier && ` · ${p.network_tier}`}
      </span>
    )
  if (p.in_network === false)
    return (
      <span className="inline-flex items-center gap-1 text-xs text-destructive">
        <ShieldX className="size-3.5" /> Out of network
      </span>
    )
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <ShieldQuestion className="size-3.5" /> Network unknown
    </span>
  )
}

function Quality({ provider: p }: { provider: Provider }) {
  if (!p.quality) return null
  const has = p.quality.score != null
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs", has ? "text-foreground" : "text-muted-foreground")}>
      <Star className={cn("size-3.5", has && "fill-amber-400 text-amber-400")} />
      {p.quality.label}
      {p.quality.source && <SourceChip source={p.quality.source} />}
    </span>
  )
}

function VsTypical({ value }: { value: Provider["price_vs_typical"] }) {
  if (!value || value === "typical")
    return value ? <div className="mt-0.5 text-xs text-muted-foreground">About typical</div> : null
  const lower = value === "lower"
  const Icon = lower ? ArrowDownRight : ArrowUpRight
  return (
    <div className={cn("mt-0.5 inline-flex items-center gap-0.5 text-xs", lower ? "text-emerald-700" : "text-amber-700")}>
      {lower ? "Lower than typical" : "Higher than typical"} <Icon className="size-3.5" />
    </div>
  )
}
