"use client"

import { useEffect, useRef } from "react"
import type { Provider } from "@/app/providers/page"

export function ProviderMap({ providers, selected, onSelect }: {
  providers: Provider[]; selected: number | null; onSelect: (id: number) => void
}) {
  const element = useRef<HTMLDivElement>(null)
  const map = useRef<import("leaflet").Map | null>(null)
  const layer = useRef<import("leaflet").LayerGroup | null>(null)
  const hasPins = providers.some((provider) => provider.latitude !== null && provider.longitude !== null)

  useEffect(() => {
    let cancelled = false
    if (!hasPins) return
    import("leaflet").then((L) => {
      if (cancelled || !element.current) return
      const first = providers.find((provider) => provider.latitude !== null && provider.longitude !== null)!
      if (!map.current) {
        map.current = L.map(element.current).setView([first.latitude!, first.longitude!], 11)
        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
          maxZoom: 18,
        }).addTo(map.current)
        layer.current = L.layerGroup().addTo(map.current)
      }
      layer.current!.clearLayers()
      const points: import("leaflet").LatLngExpression[] = []
      for (const provider of providers) {
        if (provider.latitude === null || provider.longitude === null) continue
        const point: import("leaflet").LatLngExpression = [provider.latitude, provider.longitude]
        points.push(point)
        L.circleMarker(point, { radius: provider.location_id === selected ? 10 : 7,
          color: provider.location_id === selected ? "#e46c28" : "#275dad", fillOpacity: 0.85 })
          .bindTooltip(provider.name || `NPI ${provider.npi}`).on("click", () => onSelect(provider.location_id))
          .addTo(layer.current!)
      }
      map.current.invalidateSize()
      map.current.fitBounds(L.latLngBounds(points), { padding: [24, 24], maxZoom: 14 })
    })
    return () => { cancelled = true }
  }, [providers, selected, onSelect, hasPins])

  useEffect(() => () => { map.current?.remove(); map.current = null; layer.current = null }, [])

  return <div className="space-y-2"><div ref={element} className={`${hasPins ? "" : "hidden"} h-80 w-full rounded-xl border lg:sticky lg:top-4 lg:h-[480px]`} />
    {!hasPins && <div className="flex h-80 items-center justify-center rounded-xl border p-6 text-center text-sm text-muted-foreground">No practice address in these results has a verified map coordinate.</div>}
    <p className="text-xs text-muted-foreground">Only successfully geocoded practice addresses have map markers. © OpenStreetMap contributors.</p>
  </div>
}
