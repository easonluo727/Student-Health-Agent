import { MOCK_PLANS } from "@/lib/mock-script"

export function GET() {
  return Response.json(MOCK_PLANS)
}
