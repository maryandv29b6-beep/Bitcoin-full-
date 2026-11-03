# FlowLens: build the UI with Node, run the collector + API with Python.
FROM node:22-alpine AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npx vite build --outDir /ui --emptyOutDir

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=ui /ui ./flowlens/static
ENV FLOWLENS_HOST=0.0.0.0 \
    FLOWLENS_DB_PATH=/data/flowlens.db \
    FLOWLENS_RAW_DIR=/data/raw \
    FLOWLENS_CONFIG=/config/flowlens.config.json
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "flowlens", "serve"]
