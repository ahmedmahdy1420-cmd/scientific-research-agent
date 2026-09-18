# System architecture

One modular monolith plus three small satellites, not a constellation of
microservices. The API, the Celery worker, the MCP server and the mock
clinical-trials API are the **same container image** with different commands,
so what CI tested is byte-for-byte what runs.

```mermaid
flowchart TB
    subgraph client["Client"]
        UI["React + TypeScript SPA<br/><i>Vite</i>"]
        EXT["External MCP clients<br/><i>Claude Desktop, IDEs,<br/>other internal agents</i>"]
    end

    subgraph api["FastAPI application (one image)"]
        direction TB
        MW["Middleware<br/><i>request id · body size · security headers</i>"]
        AUTH["Auth layer<br/><i>OAuth2 / JWT → Principal</i>"]
        ROUTES["REST endpoints<br/><i>Pydantic v2 request + response</i>"]
        SVC["Service layer<br/><i>agent · documents · research</i>"]

        subgraph agent["LangGraph agent"]
            direction LR
            GRAPH["State machine<br/><i>classify → plan → select →<br/>execute → analyze → verify</i>"]
            BUDGET["Budget<br/><i>iterations · tool calls · wall clock</i>"]
        end

        subgraph toollayer["Tool registry — the authorisation choke point"]
            direction TB
            REG["allowlist · permission check ·<br/>Pydantic validation · timeout · audit"]
            RAGT["RAG tools"]
            SQLT["SQL tools"]
            EXTT["External / MCP tools"]
            SENS["Sensitive tool<br/><i>needs human approval</i>"]
        end

        LLM["LLM abstraction<br/><i>OpenAI SDK · model router ·<br/>deterministic offline provider</i>"]
    end

    subgraph workers["Background"]
        CELERY["Celery worker<br/><i>ingestion · evaluation</i>"]
    end

    MCPS["MCP server<br/><i>streamable HTTP · bearer auth ·<br/>restricted service principal</i>"]
    MOCK["Mock clinical-trials API<br/><i>with failure injection</i>"]

    subgraph data["Data"]
        PG[("PostgreSQL 17<br/>+ pgvector<br/><i>HNSW cosine index</i>")]
        PGRO[("read-only role<br/><i>SQL tool layer</i>")]
        REDIS[("Redis<br/><i>cache · broker ·<br/>rate limits</i>")]
        S3[("S3 / local<br/><i>document bytes</i>")]
    end

    OPENAI{{"OpenAI API<br/><i>GPT + embeddings</i>"}}

    UI -->|"HTTPS + Bearer"| MW
    MW --> AUTH --> ROUTES --> SVC
    SVC --> GRAPH
    GRAPH --- BUDGET
    GRAPH -->|"proposes tool calls"| REG
    REG --> RAGT & SQLT & EXTT & SENS
    GRAPH -->|"prompts"| LLM
    LLM -.->|"when a key is set"| OPENAI

    EXT -->|"MCP protocol"| MCPS
    EXTT -->|"MCP protocol"| MCPS
    MCPS -->|"same service layer"| SVC

    EXTT -->|"httpx + retries"| MOCK
    RAGT --> PG
    SQLT --> PGRO
    PGRO -.->|"same instance,<br/>least privilege"| PG
    SENS --> PG

    ROUTES -->|"202 + task id"| CELERY
    CELERY --> PG
    CELERY --> S3
    CELERY -.->|"embeddings"| LLM

    SVC --> REDIS
    REG --> REDIS
    SVC --> S3
    GRAPH -->|"checkpoints"| PG

    classDef security fill:#7a2222,stroke:#f2777a,color:#fff
    classDef store fill:#1f3a5f,stroke:#5b9dff,color:#fff
    class REG,AUTH,SENS,PGRO security
    class PG,REDIS,S3,PGRO store
```

## Why this shape

**One image, several commands.** The API, worker and MCP server share all their
domain code. Splitting them into separate repositories or images would mean
three deploys to change one tool, and three chances for the tool contract to
drift.

**The tool registry is the only path to a tool.** Everything the agent can do
goes through one function that checks the allowlist, checks the caller's
permissions, validates arguments against a Pydantic model, applies a timeout
and writes an audit row. There is no second path.

**Two database roles, one instance.** Normal application access uses the owner
role. The SQL tool layer connects as `research_ro`, which Postgres itself
restricts to `SELECT`. That is a database-enforced guarantee rather than an
application promise.

**MCP is a transport, not a second implementation.** The MCP server calls the
same `ResearchService` the REST API calls. It exists so that consumers outside
this codebase get typed, discoverable tools without a bespoke integration.
