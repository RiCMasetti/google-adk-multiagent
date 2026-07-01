FROM python:3.12-slim

## For any intentional dependency upgrade, rebuild and test:
# docker compose build platform-agent
# docker compose up -d platform-agent

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
COPY platform_agent ./platform_agent
COPY common ./common
 
# Sessions directory must exist before adk_api_server starts.
# We declare it as a VOLUME mount point so docker-compose's named volume
# (or bind mount) replaces this empty directory at runtime.
RUN mkdir -p /app/sessions
 
EXPOSE 8000
 
# Use shell form so $ADK_SESSION_DB_URL is expanded at runtime.
# This lets us point at SQLite or Postgres via env without rebuilding.
CMD adk api_server \
      --host 0.0.0.0 \
      --port 8000 \
      --session_service_uri "${ADK_SESSION_DB_URL}" \
      .
