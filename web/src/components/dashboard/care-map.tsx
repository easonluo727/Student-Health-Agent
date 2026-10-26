"use client"

// Leaflet map of the providers, one price pin each. Loaded with next/dynamic (ssr: false): Leaflet needs `window`.
// The basemap is configurable (NEXT_PUBLIC_MAP_TILES, NEXT_PUBLIC_MAP_LABELS); the default is Esri's light gray
// canvas with its place-name layer on top, which needs no key (attribution required).

import "leaflet/dist/leaflet.css"
import { useEffect, useMemo } from "react"
import L from "leaflet"
import { CircleMarker, MapContainer, Marker, TileLayer, useMap } from "react-leaflet"
import type { CareQuery, Provider } from "@/lib/protocol"

const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas"
const TILES = process.env.NEXT_PUBLIC_MAP_TILES || `${ESRI}/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`
const LABELS =
  process.env.NEXT_PUBLIC_MAP_LABELS ??
  (process.env.NEXT_PUBLIC_MAP_TILES ? "" : `${ESRI}/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}`)
const ATTRIBUTION =
  process.env.NEXT_PUBLIC_MAP_ATTRIBUTION || "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors"

const whole = (n: number) => `$${Math.round(n).toLocaleString("en-US")}`

// What the user pays there; a place with no patient cost shows a dash, not its negotiated rate.
export function pinPrice(p: Provider): string | null {
  const cost = p.patient_cost?.mid
  return cost == null ? null : whole(cost)
}

function pin(p: Provider, active: boolean) {
  const price = pinPrice(p) ?? "—"
  const cls = ["price-pin", p.recommended && "is-recommended", active && "is-active"].filter(Boolean).join(" ")
  return L.divIcon({ className: cls, html: `<span>${price}</span>`, iconSize: [0, 0] })
}

function FitBounds({ points }: { points: [number, number][] }) {
  const map = useMap()
  const key = points.map((p) => p.join(",")).join(";")
  useEffect(() => {
    if (points.length === 1) map.setView(points[0], 13)
    else if (points.length > 1) map.fitBounds(L.latLngBounds(points), { padding: [48, 48], maxZoom: 14 })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refit only when the set of points changes
  }, [map, key])
  return null
}

export default function CareMap({
  query,
  providers,
  activeId,
  onHover,
  onSelect,
}: {
  query: CareQuery
  providers: Provider[]
  activeId: string | null
  onHover: (id: string | null) => void
  onSelect: (id: string) => void
}) {
  const home = query.location
  const points = useMemo<[number, number][]>(
    () => [...providers.map((p) => [p.lat, p.lng] as [number, number]), ...(home ? [[home.lat, home.lng] as [number, number]] : [])],
    [providers, home],
  )
  const center: [number, number] = home ? [home.lat, home.lng] : points[0] ?? [34.05, -118.25]

  return (
    <MapContainer center={center} zoom={11} scrollWheelZoom className="size-full" attributionControl>
      <TileLayer url={TILES} attribution={ATTRIBUTION} maxZoom={16} />
      {LABELS && <TileLayer url={LABELS} maxZoom={16} />}
      <FitBounds points={points} />
      {home && (
        <CircleMarker
          center={[home.lat, home.lng]}
          radius={7}
          pathOptions={{ color: "white", weight: 2, fillColor: "#1f6268", fillOpacity: 1 }}
        />
      )}
      {providers.map((p) => (
        <Marker
          key={p.provider_id}
          position={[p.lat, p.lng]}
          icon={pin(p, p.provider_id === activeId)}
          zIndexOffset={p.provider_id === activeId ? 1000 : p.recommended ? 500 : 0}
          eventHandlers={{
            mouseover: () => onHover(p.provider_id),
            mouseout: () => onHover(null),
            click: () => onSelect(p.provider_id),
          }}
        />
      ))}
    </MapContainer>
  )
}
