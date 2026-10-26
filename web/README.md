# web — chat frontend

Next.js 16 (App Router) + Tailwind 4 + shadcn/ui (Radix). A chat on the left and the canvas on the right: questionnaire cards, cost estimates, search steps, the plan comparison, providers near the user, and the appeal email card.

The UI is designed and built in the Lovable project **Health Coverage Hub** (https://lovable.dev/projects/b5dee4fb-ca98-482a-b613-5006ab61df02) and copied here. Change the design there, then copy `src/agent/`, `src/components/agent/` (not `DevBoardSwitcher.tsx`) and `src/styles.css` (into `src/app/globals.css`) back. `src/agent/config.ts` differs on purpose: Next reads `NEXT_PUBLIC_API_BASE`, Lovable reads `VITE_API_BASE`.

```bash
pnpm install
pnpm dev        # http://localhost:3000
pnpm lint
pnpm build      # STATIC_EXPORT=1 pnpm build writes out/, which FastAPI serves
```

## Backend

The frontend talks to the agent service through `POST {API_BASE}/turn`, which streams SSE events, plus `GET /plans`, `GET /integrations` and `POST /upload`. The contract is `docs/frontend-protocol.md`; the TypeScript types are `src/agent/protocol.ts`. Change both together.

- `NEXT_PUBLIC_API_BASE` unset → a mock transport in the browser (`src/agent/mock/`) replays scripted events. **Its numbers are placeholders.**
- `NEXT_PUBLIC_API_BASE=http://localhost:8000` → the FastAPI agent service. If the dev server isn't on port 3000, start the backend with `CORS_ORIGINS` listing that origin.

## Layout

| Path | What |
| --- | --- |
| `src/agent/protocol.ts` | Request/event types, mirrors the protocol doc |
| `src/agent/transport.ts`, `sse.ts` | HTTP transport: fetch + hand-written SSE parser |
| `src/agent/store.ts`, `useAgent.ts` | Reducer (timeline, cards, report, canvas) and the hook the UI uses |
| `src/agent/mock/` | Mock transport and the scripted demo events |
| `src/components/agent/` | Workspace, composer, cards (form, confirm, search steps, email), canvas (report, care, estimate, web search) |
| `src/app/page.tsx` | Renders the app in the browser only (the session id is random) |
