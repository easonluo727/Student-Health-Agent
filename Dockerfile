# The whole product in one image: the Next.js frontend, exported to static files, served by the FastAPI agent
# service together with its API (/api). Deployed on Railway as the agent-api service.
#
#   docker build -t student-health-agent .
#   docker run -p 8000:8000 --env-file .env student-health-agent
#
# Needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (any OpenAI-compatible endpoint). Prices come from the 3.1 GB rate
# store, which is not in the image: on Railway it is the rates-data volume at /data (RATES_DB below); locally mount it
# with -v "$PWD/data/mrf/rates.sqlite:/data/rates.sqlite:ro". Without it only flat copays are computed.
# The app database (APP_DB: accounts, conversations, transcripts, upload records) and uploaded files (UPLOAD_DIR) are
# on the same volume, so a redeploy doesn't lose them. Anonymous conversations are kept a day; a signed-in user's until
# they delete them (Google sign-in needs GOOGLE_CLIENT_ID).

FROM node:22-slim AS web
WORKDIR /web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN STATIC_EXPORT=1 NEXT_PUBLIC_API_BASE=/api pnpm build

FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=web /web/out ./web/out
ENV RATES_DB=/data/rates.sqlite APP_DB=/data/app.sqlite UPLOAD_DIR=/data/uploads PORT=8000
WORKDIR /app/workflow
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
