import type { Metadata, Viewport } from "next"
import { Inter } from "next/font/google"
import "./globals.css"

const inter = Inter({ subsets: ["latin"], weight: ["400", "600"], variable: "--font-inter" })

export const metadata: Metadata = {
  title: "TallyHealth",
  description:
    "Ask what care will cost, compare health plans, find in-network specialists, and review medical bills with sources for every number.",
}
export const viewport: Viewport = { width: "device-width", initialScale: 1 }

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={inter.variable}>
      <body>{children}</body>
    </html>
  )
}
