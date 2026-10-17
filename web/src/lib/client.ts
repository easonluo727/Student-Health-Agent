import type { ServerEvent, TurnRequest } from "./protocol"
import { readSSE } from "./sse"

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "/api/mock"
export const IS_MOCK = !process.env.NEXT_PUBLIC_API_BASE

// POST a turn and yield SSE events as they arrive. EventSource only does GET,
// so the stream is parsed by hand.
export async function* streamTurn(
  request: TurnRequest,
  signal?: AbortSignal,
): AsyncGenerator<ServerEvent> {
  const response = await fetch(`${API_BASE}/turn`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(request),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`Server returned ${response.status}`)
  }
  for await (const event of readSSE(response.body)) {
    yield event as ServerEvent
  }
}
