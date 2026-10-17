import { scriptFor, searchIntro, searchReply, searchTarget, type MockStep } from "@/lib/mock-script"
import type { SearchSummary, TurnRequest } from "@/lib/protocol"
import { readSSE } from "@/lib/sse"

// Live search worker (search_worker/worker.py). Unset → the scripted search.
const SEARCH_WORKER_URL = process.env.SEARCH_WORKER_URL

export async function POST(request: Request) {
  const body = (await request.json()) as TurnRequest
  const encoder = new TextEncoder()

  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: string, data: unknown) =>
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
      const play = async (steps: MockStep[]) => {
        for (const step of steps) {
          if ("pause" in step) await new Promise((resolve) => setTimeout(resolve, step.pause))
          else send(step.event, step.data)
        }
      }

      const insurer = searchTarget(body)
      if (insurer && SEARCH_WORKER_URL) {
        await play(searchIntro(insurer))
        await liveSearch(insurer, send, play)
      } else {
        await play(scriptFor(body))
      }
      send("done", {})
      controller.close()
    },
  })

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  })
}

// Relay the worker's steps as they happen, then reply from its summary.
async function liveSearch(
  insurer: string,
  send: (event: string, data: unknown) => void,
  play: (steps: MockStep[]) => Promise<void>,
) {
  const searchId = `search_${Date.now().toString(36)}`
  let summary: SearchSummary | null = null
  try {
    const response = await fetch(`${SEARCH_WORKER_URL}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ insurer }),
    })
    if (!response.ok || !response.body) throw new Error(`worker returned ${response.status}`)
    for await (const { event, data } of readSSE(response.body)) {
      if (event === "search_step") send("search_step", { ...(data as object), search_id: searchId })
      else if (event === "search_summary") summary = data as SearchSummary
    }
  } catch {
    send("search_step", {
      search_id: searchId,
      step_id: "worker",
      status: "failed",
      text: "The live search service isn't reachable",
    })
  }
  await play(
    searchReply(
      summary ?? {
        insurer,
        status: "error",
        message: `I couldn't search for ${insurer} right now.`,
        facts: [],
        pages: [],
      },
    ),
  )
}
