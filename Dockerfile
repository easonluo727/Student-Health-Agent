# The whole product in one image: the Next.js frontend, exported to static files, served by the FastAPI agent
# service together with its API (/api). Deployed on Railway as the agent-api service.
#
#   docker build -t student-health-agent .
#   docker run -p 8000:8000 --env-file .env -v "$PWD/data/mrf/rates.sqlite:/data/rates.sqlite:ro" student-health-agent
#
# Needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (any OpenAI-compatible endpoint). The 3.1 GB rates database is not in the image: mount it at
# /data/rates.sqlite (on Railway, the rates-data volume); without it the agent uses the aggregate allowed amounts.

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
ENV BRANCH_A_RATES_SQLITE=/data/rates.sqlite PORT=8000
WORKDIR /app/workflow
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
