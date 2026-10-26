"use client"

// How the service's price spreads across the network's providers, with the midpoint marked.
// Bins come from the backend (PriceSummary.bins); nothing is computed here but the axis labels.

import { Area, AreaChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import type { PriceSummary } from "@/lib/protocol"
import { usd } from "@/components/chat/source-chip"

export function PriceChart({ price }: { price: PriceSummary }) {
  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={price.bins} margin={{ top: 28, right: 8, bottom: 0, left: 8 }}>
            <defs>
              <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.55} />
                <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0.08} />
              </linearGradient>
            </defs>
            <XAxis dataKey="price" type="number" domain={[price.min, price.max]} hide />
            <YAxis hide />
            <Tooltip
              cursor={{ stroke: "var(--border)" }}
              contentStyle={{ borderRadius: 8, border: "1px solid var(--border)", fontSize: 12 }}
              formatter={(count) => [`${count} providers`, ""]}
              labelFormatter={(value) => `Around ${usd(Number(value))}`}
            />
            <Area
              type="monotone"
              dataKey="count"
              stroke="var(--chart-1)"
              strokeWidth={2}
              fill="url(#priceFill)"
              isAnimationActive={false}
            />
            <ReferenceLine
              x={price.midpoint}
              stroke="var(--foreground)"
              strokeDasharray="4 4"
              label={{ value: usd(price.midpoint), position: "top", fontSize: 12, fontWeight: 600, fill: "var(--foreground)" }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-1 flex justify-between px-2 text-[11px] text-muted-foreground tabular-nums">
        <span>
          min <span className="font-medium text-foreground">{usd(price.min)}</span>
        </span>
        <span>Price</span>
        <span>
          max <span className="font-medium text-foreground">{usd(price.max)}</span>
        </span>
      </div>
    </div>
  )
}
