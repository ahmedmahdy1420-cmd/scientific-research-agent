# Agent workflow

The graph in `app/agents/graph.py`. Every box is a node; every diamond is a
routing function that is a pure function of state (and therefore unit tested
in `tests/unit/test_agent_routing.py`).

```mermaid
flowchart TD
    START([START]) --> CLASSIFY["classify_question<br/><i>fast model · structured output</i>"]

    CLASSIFY --> R1{category?}
    R1 -->|small talk / out of scope| DIRECT["direct_answer<br/><i>no tools, no retrieval</i>"]
    R1 -->|anything else| PLAN["plan<br/><i>reasoning model → AgentPlan</i>"]

    PLAN --> SELECT["select_tools<br/><b>authorisation happens here</b><br/><i>drop unknown · drop unpermitted ·<br/>truncate to budget</i>"]

    SELECT --> R2{which groups?}
    R2 -->|sensitive action| APPROVAL
    R2 -->|documents| RAG["rag_tools"]
    R2 -->|experiments / compounds| SQL["sql_tools"]
    R2 -->|trials / MCP| EXT["external_tools"]
    R2 -->|nothing authorised| ANALYZE
    R2 -->|budget exhausted| FINAL

    RAG --> ANALYZE["analyze<br/><i>synthesis from fenced evidence only</i>"]
    SQL --> ANALYZE
    EXT --> ANALYZE

    ANALYZE --> R3{synthesis ok?}
    R3 -->|no| FAIL["handle_failure<br/><i>safe, honest response</i>"]
    R3 -->|yes| VERIFY["verify<br/><i>deterministic citation check<br/>+ LLM grounding check</i>"]

    VERIFY --> R4{recommendation}
    R4 -->|accept| FINAL["finalize"]
    R4 -->|answer_with_caveats| FINAL
    R4 -->|refuse| FINAL
    R4 -->|retrieve_more| R5{budget left AND<br/>last retry found<br/>something new?}
    R5 -->|no| FINAL
    R5 -->|yes| MORE["retrieve_more<br/><i>widen the query, drop filters</i>"]
    MORE --> RAG

    APPROVAL["approval_gate<br/><b>interrupt()</b><br/><i>state checkpointed to Postgres;<br/>the process can restart here</i>"]
    APPROVAL --> R6{human decision}
    R6 -->|rejected| FINAL
    R6 -->|approved| SENS["sensitive_tools<br/><i>runs the ONE approved action</i>"]
    SENS --> FINAL

    DIRECT --> END([END])
    FINAL --> END
    FAIL --> END

    classDef gate fill:#7a2222,stroke:#f2777a,color:#fff
    classDef pause fill:#6b5400,stroke:#e3b341,color:#fff
    class SELECT,SENS gate
    class APPROVAL pause
```

## The three branches that matter

**Fan-out.** `route_tools` returns a *list* of node names, so LangGraph starts
those branches concurrently. A question needing both literature and trial data
pays for one round trip, not two. The concurrent branches all write to
`tool_results` and `evidence`, which is why those state keys carry
`operator.add` reducers.

**The retrieval loop is doubly bounded.** It stops on the iteration budget
*and* on "the last retry added no new evidence" — re-running a query that
already returned nothing only costs latency and tokens.

**The approval pause is a real pause.** `interrupt()` checkpoints the entire
state to Postgres and returns control. The run resumes minutes later, possibly
in a different container, with `Command(resume=...)`. That constraint is why
the agent state is strictly JSON-serialisable.

## Bounded autonomy

| Bound | Setting | What it prevents |
|---|---|---|
| Iterations | `MAX_AGENT_ITERATIONS` | An infinite "evidence is insufficient" loop |
| Tool calls | `MAX_TOOL_CALLS` | Fan-out across the whole run |
| Wall clock | `MAX_RUNTIME_SECONDS` | One slow dependency hanging a request |
| Supersteps | `recursion_limit=40` | A cycle the budget logic somehow misses |

Exceeding a budget is a first-class outcome (`status=budget_exceeded`), not an
exception: the run returns the evidence it did gather and says it was truncated.
