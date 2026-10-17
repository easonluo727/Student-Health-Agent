# web — chat frontend

Next.js 16 (App Router) + Tailwind 4 + shadcn/ui (Base UI). The chat window, questionnaire cards, cost cards, search progress and the Insurance Arena dashboard.

```bash
pnpm install
pnpm dev        # http://localhost:3000
pnpm lint
pnpm build
```

## Backend

The frontend talks to the agent service through one endpoint, `POST {API_BASE}/turn`, which streams SSE events. The contract is `docs/frontend-protocol.md`; the TypeScript types are `src/lib/protocol.ts`. Change both together.

- `NEXT_PUBLIC_API_BASE` unset → built-in mock at `/api/mock` (`src/app/api/mock/turn/route.ts`), which replays the demo flow from `src/lib/mock-script.ts`. **Its numbers are placeholders**, and the UI shows a "Mock backend" badge.
- `NEXT_PUBLIC_API_BASE=http://localhost:8000` → the FastAPI agent service. See `.env.example`.

## Layout

| Path | What |
| --- | --- |
| `src/lib/protocol.ts` | Request/event types, mirrors the protocol doc |
| `src/lib/client.ts` | `streamTurn()`: POST + hand-rolled SSE parser |
| `src/lib/chat-state.ts` | Reducer: timeline items, card states, report |
| `src/components/chat/` | Chat shell and cards: form, confirm, estimate, search, source chips |
| `src/components/dashboard/arena-panel.tsx` | Recommendation, scenario cost bars, comparison table |
