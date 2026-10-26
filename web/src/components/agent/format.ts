import type { CostRange } from "@/agent/protocol";

// Display formatting only. Never derive new numbers here.
export function money(n: number): string {
  const cents = !Number.isInteger(n);
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 }).format(n);
}
export function costText(c: CostRange | null | undefined, empty = "Price unknown"): string {
  if (!c) return empty;
  if (c.low != null && c.high != null && c.low !== c.high) return `${money(c.low)}–${money(c.high)}`;
  return money(c.mid);
}
export function initials(s: string): string {
  return s.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]!.toUpperCase()).join("");
}
