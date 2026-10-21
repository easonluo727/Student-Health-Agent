import { scriptFor, type MockStep } from "@/lib/mock-script"
import type { TurnRequest } from "@/lib/protocol"

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

      await play(scriptFor(body))
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

