# Authentication and authorisation

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant API as FastAPI
    participant DB as PostgreSQL
    participant AG as LangGraph agent
    participant TR as Tool registry
    participant T as Tool

    C->>API: POST /auth/login { email, password }
    API->>API: rate limit by client IP
    API->>DB: SELECT user WHERE email
    alt no such user
        API->>API: verify against a dummy hash
        Note over API: constant-ish timing, so response<br/>time does not reveal which accounts exist
    end
    API->>API: Argon2id verify
    API-->>C: { access_token, refresh_token }

    C->>API: POST /chat  (Authorization: Bearer ...)
    API->>API: decode JWT<br/>(explicit algorithm allowlist — alg=none is rejected)
    API->>DB: load the user row
    Note over API,DB: The token carries roles, but the row is still<br/>read: a disabled account stops working at once,<br/>not at token expiry
    API->>API: build Principal (roles, permissions, clearance)

    API->>AG: run(question, principal)
    AG->>AG: classify → plan
    AG->>TR: "the model proposes retrieve_documents(...)"

    rect rgb(122, 34, 34)
        Note over TR: THE CHOKE POINT — ordinary Python,<br/>no model involvement
        TR->>TR: 1. is it on the allowlist?
        TR->>TR: 2. does THIS principal hold the permission?
        TR->>TR: 3. do the arguments validate (Pydantic)?
        TR->>TR: 4. sensitive and unapproved? → refuse
    end

    alt denied
        TR->>DB: audit row (status=denied, reason)
        TR-->>AG: failed ToolResult
        Note over AG: The agent continues and says<br/>what it could not do
    else allowed
        TR->>T: execute under a timeout
        T->>DB: query WITH access_level IN (caller's levels)
        T-->>TR: results
        TR->>DB: audit row (status=executed, latency, count)
        TR-->>AG: ToolResult + evidence
    end
```

## The rule the whole design rests on

**The LLM is not the security boundary.**

The model may *request* a tool call. Whether that call runs is decided in
`app/tools/registry.py` against the authenticated principal. There is no code
path where a string produced by a model, or found in a retrieved document, can
cause step 2 to be skipped.

## Layers

| Layer | Mechanism | Where |
|---|---|---|
| Authentication | OAuth2 bearer, JWT HS256, explicit algorithm allowlist | `app/core/security.py` |
| Password storage | Argon2id via pwdlib | `app/core/security.py` |
| Role → permissions | Flat permission strings on the role row | `app/auth/permissions.py` |
| Route authorisation | `require_permissions(...)` dependency | `app/auth/dependencies.py` |
| Tool authorisation | Registry check before execution | `app/tools/registry.py` |
| Row filtering | `access_level IN (...)` in every query | `app/rag/retriever.py`, `app/tools/sql_tools.py` |
| Resource ownership | `owns_or_can_read_any` | `app/auth/permissions.py` |
| Write path | Human approval + separate permission | `app/tools/sensitive.py` |
| Database | A role Postgres restricts to `SELECT` | `docker/postgres/init.sql` |

## The role matrix

| | researcher | senior_researcher | admin |
|---|---|---|---|
| Read public + internal documents | yes | yes | yes |
| Read restricted documents | **no** | yes | yes |
| Upload documents | yes | yes | yes |
| Run the agent | yes | yes | yes |
| Approve sensitive actions | **no** | yes | yes |
| Delete/archive documents | **no** | yes | yes |
| Sensitive tools permission | **no** | **no** | yes |
| Read anyone's agent runs | **no** | **no** | yes |

The roles are strictly nested, and a unit test asserts that
(`test_roles_are_strictly_nested`) so a permission cannot be added to one role
and silently forgotten in another.

## Three independent controls on a write

A sensitive action must clear all three:

1. **Human approval** — the run pauses at `approval_gate` and cannot proceed
   without an explicit decision.
2. **Segregation of duties** — the approver must not be the requester.
3. **Permission** — approval is not a substitute for authorisation. A
   researcher whose archive request is approved by an admin *still* fails,
   because the researcher lacks `documents:delete`. This is verified in
   `test_approval_does_not_grant_missing_permissions`.
