"use client"

import { useEffect, useState, type FormEvent } from "react"
import Link from "next/link"
import "leaflet/dist/leaflet.css"
import { ProviderMap } from "@/components/providers/provider-map"

type Plan = { plan_id: string; name: string; confirmed_tiers: (string | null)[] }
type Scenario = { scenario: string; label: string }
type Price = {
  rate_available: boolean; negotiated_rate: number | null; patient_cost: number | null
  estimate_status: string; rate_status: string; network_verification: string
  missing_fields?: string[]
  provenance?: { negotiated_type_mix?: string }
}
export type Provider = {
  npi: string; location_id: number; name: string | null; address: string; phone: string | null
  latitude: number | null; longitude: number | null; distance_miles: number | null
  geocode_precision: string | null; distance_status: string; map_destination: string
  primary_taxonomy: string | null; location_type: string; network_verification: string
  location_rate_scope: string; prices: Record<string, Price>
}
type Result = {
  status: string; message?: string; providers: Provider[]; total?: number
  candidate_truncated?: boolean; sort_scope?: string
  plans: { plan_id: string; name: string; network_tier: string | null; plan_network_status: string }[]
  user_position?: { latitude: number | null; longitude: number | null; precision: string }
  provenance?: { nppes_release_date: string | null; nppes_source_url: string | null }
}

const BASE = process.env.NEXT_PUBLIC_API_BASE || "/api"
const dollars = (value: number) => `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const pretty = (value: string) => value.replaceAll("_", " ")

export default function ProvidersPage() {
  const [options, setOptions] = useState<{ scenarios: Scenario[]; plans: Plan[] } | null>(null)
  const [zip, setZip] = useState("")
  const [scenario, setScenario] = useState("office_visit_established")
  const [radius, setRadius] = useState(10)
  const [deductible, setDeductible] = useState("")
  const [selectedPlans, setSelectedPlans] = useState<string[]>([])
  const [tiers, setTiers] = useState<Record<string, string>>({})
  const [sort, setSort] = useState("nearest")
  const [result, setResult] = useState<Result | null>(null)
  const [selectedLocation, setSelectedLocation] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    fetch(`${BASE}/providers/options`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`Provider API unavailable (${response.status})`)
        return response.json()
      })
      .then(setOptions)
      .catch((cause) => setError(cause instanceof Error ? cause.message : "Provider API unavailable"))
  }, [])

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError("")
    try {
      const params = new URLSearchParams({ zip_code: zip.trim(), scenario, radius_miles: String(radius), sort })
      if (deductible !== "") params.set("remaining_deductible", deductible)
      for (const plan of selectedPlans) {
        params.append("plan_ids", plan)
        if (tiers[plan] !== undefined) params.append("plan_tiers", `${plan}|${tiers[plan]}`)
      }
      const response = await fetch(`${BASE}/providers/search?${params}`)
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail || `Search failed (${response.status})`)
      setResult(body)
      setSelectedLocation(body.providers[0]?.location_id ?? null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Search failed")
    } finally {
      setBusy(false)
    }
  }

  return <main className="mx-auto max-w-7xl space-y-6 px-4 py-7 sm:px-6">
    <div className="space-y-2">
      <Link href="/" className="text-sm text-primary underline">← Health Insurance Agent</Link>
      <h1 className="text-2xl font-semibold">Find nearby providers</h1>
      <p className="max-w-3xl text-sm text-muted-foreground">Practice locations come from NPPES. Rates, when available, are NPI-specific negotiated prices; they do not prove current network participation or a final paid amount.</p>
    </div>
    <form onSubmit={search} className="space-y-4 rounded-xl border p-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <label className="text-sm">Your ZIP code
          <input required pattern="[0-9]{5}" value={zip} onChange={(e) => setZip(e.target.value)} placeholder="90024" className="mt-1 w-full rounded-md border bg-background p-2" />
        </label>
        <label className="text-sm">Service
          <select value={scenario} onChange={(e) => setScenario(e.target.value)} className="mt-1 w-full rounded-md border bg-background p-2">
            {options?.scenarios.map((item) => <option key={item.scenario} value={item.scenario}>{item.label}</option>)}
          </select>
        </label>
        <label className="text-sm">Radius (miles)
          <input type="number" min="1" max="100" value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="mt-1 w-full rounded-md border bg-background p-2" />
        </label>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="text-sm">Remaining deductible (optional; only a number you know)
          <input type="number" min="0" step="0.01" value={deductible} onChange={(e) => setDeductible(e.target.value)} placeholder="Unknown" className="mt-1 w-full rounded-md border bg-background p-2" />
        </label>
        <label className="text-sm">Sort
          <select value={sort} onChange={(e) => setSort(e.target.value)} className="mt-1 w-full rounded-md border bg-background p-2">
            <option value="nearest">Nearest</option><option value="lowest_cost">Lowest calculated patient cost</option>
          </select>
        </label>
      </div>
      <fieldset className="space-y-2"><legend className="text-sm font-medium">Compare plans (optional)</legend>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{options?.plans.map((plan) => <div key={plan.plan_id} className="rounded-md border p-2 text-sm">
          <label className="flex gap-2"><input type="checkbox" checked={selectedPlans.includes(plan.plan_id)} onChange={(e) => setSelectedPlans(e.target.checked ? [...selectedPlans, plan.plan_id] : selectedPlans.filter((id) => id !== plan.plan_id))} />{plan.name}</label>
          {selectedPlans.includes(plan.plan_id) && plan.confirmed_tiers.length > 1 && <select aria-label={`${plan.name} network tier`} value={tiers[plan.plan_id] ?? ""} onChange={(e) => setTiers({ ...tiers, [plan.plan_id]: e.target.value })} className="mt-2 w-full rounded border bg-background p-1">
            <option value="">Select network tier</option>{plan.confirmed_tiers.map((tier) => <option key={tier ?? ""} value={tier ?? ""}>{tier || "Default"}</option>)}
          </select>}
        </div>)}</div>
      </fieldset>
      <button disabled={busy || !options} className="rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50">{busy ? "Searching…" : "Search locations"}</button>
    </form>
    {error && <p role="alert" className="rounded-md border border-red-300 p-3 text-sm text-red-700">{error}. Connect the real backend with NEXT_PUBLIC_API_BASE.</p>}
    {result && <section className="space-y-4">
      <p className="text-sm text-muted-foreground">{result.status === "ok" ? `${result.total} candidate locations${result.candidate_truncated ? " (search capped; expand or narrow the radius)" : ""}. ${result.sort_scope === "nearest_200_candidates" ? "Lowest-cost sort covers only the nearest 200 candidates. " : ""}ZIP position: ${pretty(result.user_position?.precision || "unknown")}. NPPES release: ${result.provenance?.nppes_release_date || "unknown"}.` : result.message}</p>
      {result.status === "ok" && <div className="grid gap-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(300px,1fr)]">
        <div className="overflow-x-auto rounded-xl border"><table className="w-full min-w-[570px] text-left text-sm"><thead className="bg-muted/50"><tr><th className="p-3">Provider & location</th><th className="p-3">Distance</th>{result.plans.map((plan) => <th key={plan.plan_id} className="p-3">{plan.name}{plan.network_tier ? ` · ${plan.network_tier}` : ""}</th>)}</tr></thead>
          <tbody>{result.providers.map((provider) => <tr key={provider.location_id} onClick={() => setSelectedLocation(provider.location_id)} className={`cursor-pointer border-t ${selectedLocation === provider.location_id ? "bg-primary/10" : ""}`}>
            <td className="p-3"><div className="font-medium">{provider.name || `NPI ${provider.npi}`}</div><div className="text-xs text-muted-foreground">NPI {provider.npi} · {provider.location_type}</div><a href={provider.map_destination} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} className="text-primary underline">{provider.address}</a>{provider.phone && <div>{provider.phone}</div>}</td>
            <td className="p-3">{provider.distance_miles === null ? "Unavailable" : `≈ ${provider.distance_miles} mi`}</td>
            {result.plans.map((plan) => { const price = provider.prices[plan.plan_id]; return <td key={plan.plan_id} className="p-3">{price?.patient_cost !== null && price?.patient_cost !== undefined ? <strong>{dollars(price.patient_cost)} estimated</strong> : <span>{pretty(price?.estimate_status || plan.plan_network_status)}</span>}{price?.negotiated_rate != null && <div className="text-xs text-muted-foreground">{dollars(price.negotiated_rate)} MRF dollar rate{price.provenance?.negotiated_type_mix && ` (${price.provenance.negotiated_type_mix})`}</div>}<div className="text-xs text-muted-foreground">Network unverified</div></td> })}
          </tr>)}</tbody></table>{result.providers.length === 0 && <p className="p-4 text-sm">No matching practice locations in this radius or ZIP.</p>}</div>
        <ProviderMap providers={result.providers} selected={selectedLocation} onSelect={setSelectedLocation} />
      </div>}
      <p className="text-xs text-muted-foreground">NPPES addresses and Census-geocoded pins may be stale or interpolated. A rate belongs to an NPI, not necessarily every listed office. Verify location, billing provider, network, coverage and benefits with the plan. No cash or network-median price is shown.</p>
    </section>}
  </main>
}
