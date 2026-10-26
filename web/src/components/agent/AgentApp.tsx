"use client"

import { useMemo } from "react"
import { createTransport } from "@/agent/transport"
import { useAgent } from "@/agent/useAgent"
import { AgentWorkspace } from "./AgentWorkspace"

export default function AgentApp() {
  // NEXT_PUBLIC_API_BASE set → the FastAPI agent; unset → the built-in mock replay.
  const transport = useMemo(() => createTransport(), [])
  const agent = useAgent(transport)
  return <AgentWorkspace agent={agent} />
}
