# MCP integration

```mermaid
flowchart LR
    subgraph consumers["MCP clients"]
        CD["Claude Desktop<br/><i>stdio</i>"]
        IDE["IDE assistant<br/><i>stdio</i>"]
        OTHER["Another internal agent<br/><i>streamable HTTP</i>"]
        SELF["This agent's own<br/>mcp_* tools<br/><i>streamable HTTP</i>"]
    end

    subgraph server["MCP server (python -m app.mcp)"]
        AUTHM["Bearer-token middleware<br/><i>constant-time compare</i>"]
        DNS["DNS-rebinding protection<br/><i>Host header allowlist</i>"]
        TOOLS["Tools<br/>search_research_data<br/>get_experiment<br/>search_literature<br/>get_trial_information"]
        PRIN["Service principal<br/><b>no restricted access</b><br/><b>no sensitive tools</b><br/><b>no write permissions</b>"]
    end

    subgraph shared["Shared application code"]
        SVC["ResearchService"]
        RAG["RAGPipeline"]
        EXTC["ClinicalTrialsClient"]
    end

    DB[("PostgreSQL + pgvector")]
    REST["FastAPI REST endpoints"]

    CD & IDE & OTHER & SELF --> AUTHM
    AUTHM --> DNS --> TOOLS
    TOOLS --- PRIN
    TOOLS --> SVC & RAG & EXTC
    REST --> SVC & RAG & EXTC
    SVC & RAG --> DB

    classDef security fill:#7a2222,stroke:#f2777a,color:#fff
    class AUTHM,DNS,PRIN security
```

## Why MCP is here at all

The agent in this repository could call `app/tools/sql_tools.py` directly — and
for its own tools it does. MCP earns its place when the **consumer is not this
codebase**: Claude Desktop, an IDE, a colleague's agent, a second internal
service.

Without it, each of those needs a bespoke integration against our REST API,
plus its own auth handling, plus its own idea of what the arguments mean. With
it, they speak one protocol, **discover the tools and their JSON Schemas at
runtime**, and get typed results back.

The honest summary: MCP is USB-C for tools. The value is not that it is
technically novel — it is that it is a standard, so N clients times M tools
stops being N×M integrations.

## The rule this implementation follows

**MCP is a transport, not a second implementation.** Every tool in
`app/mcp/server.py` is a thin adapter over the same service layer the REST API
uses. There is no business logic in the MCP server that exists nowhere else,
which is what stops the two surfaces drifting apart.

## Security

An MCP server is a remotely callable surface, so it is treated as one:

* **Authentication** — streamable HTTP behind a bearer-token middleware using a
  constant-time comparison. Unauthenticated requests get a 401, verified in
  the test suite.
* **A restricted identity** — the server resolves that token to a *service
  principal* with a fixed, narrow permission set. It deliberately cannot read
  restricted documents, cannot read restricted experiments, and has no access
  to the sensitive-action tool. A leaked MCP token is therefore limited to the
  public and internal corpus. `test_service_principal_holds_no_write_or_sensitive_permissions`
  pins this down.
* **DNS-rebinding protection** — the SDK's Host header check stays on; the
  legitimate hostnames are configured (`MCP_ALLOWED_HOSTS`) rather than the
  check being disabled.
* **Transport isolation** — in AWS the MCP service has no load balancer and no
  public route. It is reachable only inside the VPC via service discovery.

In production the static bearer would be replaced by an OAuth2 access token
from the same issuer as the REST API. Only `BearerTokenTransport` and the
middleware would change.

## Connecting a desktop client

```json
{
  "mcpServers": {
    "scientific-research": {
      "command": "python",
      "args": ["-m", "app.mcp", "--transport", "stdio"],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://research:research@localhost:5432/research",
        "REDIS_URL": "redis://localhost:6379/0"
      }
    }
  }
}
```

Over the network instead:

```bash
python -m app.mcp --transport streamable-http   # http://localhost:8020/mcp
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" http://localhost:8020/mcp
```
