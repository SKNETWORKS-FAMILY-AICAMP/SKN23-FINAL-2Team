# MCP Server Dockerfile
# 담당: 양창일 (INF-06)

FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

COPY . .

CMD ["uvicorn", "mcp_server.server:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
