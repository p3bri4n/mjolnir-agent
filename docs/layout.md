# Layout

```
docker-compose.yml
.env.example
requirements-test.txt   shared test dependencies (pytest, respx)
services/
  langgraph-agent/   OpenAI-compatible API + LangGraph graph (autonomy,
                     human supervision — see docs/architecture/)
    app/
    tests/
  skill-manager/      lists/matches skills (./skills)
    app/
    tests/
  context-manager/    RAG + memory (Qdrant + sentence-transformers)
    app/
    tests/
  mcp-client/          spawns filesystem on demand (docker.sock) ; browser
                       is a persistent HTTP server (mcp-client connects to
                       it over Streamable HTTP)
    app/
    tests/
  playwright-mcp/      official mcp/playwright image, browser driven by
                       the agent (separate docker-compose service, native HTTP
                       server — see docs/resolved-bugs.md)
  ocr-service/         OCR capability (PaddleOCR CPU), POST /ocr — not an
                       MCP server; currently has no caller in the
                       codebase (see README, "Known, accepted limitations")
    app/
    tests/
  dashboard/           local observability cockpit — see
                       docs/architecture/observability.md
    app/
      static/          vanilla HTML/JS page served as-is (no build step)
    tests/
skills/     to be filled in (one subfolder per skill, each with a SKILL.md)
workspace/  shared with the filesystem MCP server, and with langgraph-agent
            for the audit log (.audit/, see
            docs/architecture/tool-supervision.md)
models/     weights (exl3) of the model and multimodal projector served by
            tabbyAPI — never downloaded automatically, see
            docs/architecture/inference-backend.md
```
