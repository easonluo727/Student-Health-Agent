"use client"

import dynamic from "next/dynamic"

// The UI comes from the Lovable project "Health Coverage Hub" (src/agent, src/components/agent). It renders in the
// browser only: the session id is a random UUID, which must not be fixed at build time.
const AgentApp = dynamic(() => import("@/components/agent/AgentApp"), { ssr: false })

export default function Page() {
  return (
    <main className="h-dvh bg-background text-foreground">
      <AgentApp />
    </main>
  )
}
