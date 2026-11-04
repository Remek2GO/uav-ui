FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/uav_ui ./uav_ui
COPY spec /app/spec
COPY --from=frontend /build/dist /app/frontend/dist
ENV UAV_UI_DATA=/data UAV_UI_STATIC=/app/frontend/dist UAV_UI_SCHEMA=/app/spec/plugin.schema.json
EXPOSE 8090
CMD ["uvicorn", "--factory", "uav_ui.main:create_app", "--host", "127.0.0.1", "--port", "8090", "--no-server-header"]
