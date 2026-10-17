import type { ServerEvent, TurnRequest } from "./protocol"

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

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ""
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value.replace(/\r\n/g, "\n")
    let boundary: number
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const parsed = parseBlock(block)
      if (parsed) yield parsed
    }
  }
}

function parseBlock(block: string): ServerEvent | null {
  let event = "message"
  const dataLines: string[] = []
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim()
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart())
  }
  if (dataLines.length === 0) return null
  return { event, data: JSON.parse(dataLines.join("\n")) } as ServerEvent
}
