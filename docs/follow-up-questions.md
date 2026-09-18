# Likely follow-up questions

Not generic agent questions — these are the ones an interviewer asks *after*
seeing **this** architecture, usually because they have spotted something and
want to know whether you spotted it too.

The right posture for all of them: name the trade-off before they do.

---

## On the agent graph

**"Your graph has a retrieval loop. What stops it running forever?"**

Three things, independently. `MAX_AGENT_ITERATIONS` caps the loop.
`MAX_TOOL_CALLS` caps total execution across all iterations.
`MAX_RUNTIME_SECONDS` is a wall-clock deadline — which is the one that actually
matters, because the other two can both be satisfied while a single slow
dependency hangs. There is also `recursion_limit=40` on the compiled graph as a
backstop.

Beyond the budgets there is a behavioural guard: if a retry produced no *new*
evidence, the router stops. Re-running a query that already returned nothing
buys latency and tokens and nothing else. I added that after watching a run
burn its whole iteration budget on three identical empty retrievals.

---

**"Why is classification a separate LLM call? Couldn't the planner do both?"**

It could, and that would be one fewer round trip. I split them because they
want different models: classification is short and schema-constrained and runs
fine on the small model, planning is not. Merging them means paying
reasoning-model prices for the routing decision on every request, including the
ones that turn out to need no tools at all.

The measurable case: "what can you do?" is classified as small talk and answered
with one fast call. Merged, it would cost a full planning call to discover it
needed nothing.

The trade-off is one extra round trip (~200-400ms) on questions that do need
tools. At higher volume I would measure whether a fine-tuned small classifier
beats the general model on both axes.

---

**"What happens if the classifier is wrong?"**

It degrades rather than fails. Classification is a routing *hint*, not the
answer: if the structured-output call fails entirely, the node defaults to the
literature branch, which is the most generally useful. If it picks the wrong
category, the planner still proposes tools from the full permitted set, and
`select_tools` still authorises them — so a misclassification costs relevance,
not correctness or safety.

The case that would worry me is a *sensitive* request classified as ordinary.
That is why the sensitive gate lives in the tool registry as well: even if the
classifier misses it, `request_sensitive_action` cannot execute without an
approval in `ToolContext.approved_actions`.

---

**"Your fan-out writes to shared state from concurrent nodes. Why doesn't that
race?"**

Because every key written by more than one concurrent node carries a reducer.
`tool_results`, `evidence` and `steps` use `operator.add`; `usage` uses a custom
merge that sums counters and unions the model list. Without those, LangGraph
raises `InvalidUpdateError` on the concurrent write — it fails loudly rather
than silently losing one branch.

The related trap, which is a real one: the tools themselves run concurrently
against the database, and a single `AsyncSession` is **not** safe to share
across concurrent tasks. That is why `ToolContext` carries a session *factory*
and each execution opens its own session.

---

## On retrieval

**"Your similarity threshold is 0.05. Isn't that so low it's meaningless?"**

Nearly, and deliberately. Cosine distributions are model-specific — a threshold
tuned for `text-embedding-3-large` silently discards valid hits from a
different model, and I have the offline provider as a second model in the same
codebase. The floor only removes obvious noise; top-K plus reranking does the
real filtering.

I started at 0.15 and it cost me: longer queries diluted similarity below the
floor and the agent got zero evidence for a question the corpus clearly
answered. That is the failure mode of a tuned-by-intuition threshold. The right
way to set it is from evaluation data with recall@k, which I have not built
yet — so I kept it low and let K do the work.

---

**"Fixed-size chunking is pretty naive. Why not semantic chunking?"**

It is, and I would expect semantic or layout-aware chunking to beat it. What I
have is paragraph-aware packing with a token overlap, section and page tracking
(so a citation says "page 4, Results"), and — importantly — splitting of
paragraphs that are themselves oversized.

That last part is not hypothetical. Cleaned academic PDFs regularly contain
multi-page paragraphs, and my first implementation produced a single
3800-token chunk for such a document: too big to embed usefully and useless for
retrieval, because the whole thing came back for any query matching one
sentence in it. A unit test pins that now.

What I would not claim is that this is optimal. Without recall@k I cannot prove
semantic chunking is better *here*, which is exactly the measurement I would
build before changing it.

---

**"Dense retrieval is bad at exact identifiers. Doesn't that break your
compound queries?"**

Yes, and that is why compound and experiment lookups do not go through RAG at
all. "Which experiments used CMP-0001" is a relational question with an exact
answer, so it routes to `search_experiments_by_compound`, which is a SQL tool
with a foreign key and an index.

Where it still bites is a *literature* question that happens to contain a code.
The fix is hybrid retrieval — BM25 or a `tsvector` column alongside the vector
search, fused with reciprocal rank fusion — and it is the first item on my
retrieval roadmap for exactly this reason.

---

**"You cap chunks per document at three. What if the answer needs five?"**

Then it gets four sources instead of one document's five chunks, and the answer
says what it could not cover. The cap exists because without it one verbose
paper fills the context window and crowds out contradicting evidence — which for
a *research* assistant is the worse failure: an answer that looks
well-supported because it read one paper thoroughly.

If the evaluation suite showed completeness suffering, I would raise K rather
than the per-document cap, so the breadth improves rather than the depth of one
source.

---

## On security

**"Your rate limiter fails open. Isn't that a vulnerability?"**

It is a trade-off I made explicitly, and I would make the same one again for
this system. If Redis is unavailable, `RateLimiter.check` logs and returns
rather than rejecting. The alternative — failing closed — means a cache outage
takes the entire API down.

For this system, unthrottled traffic during a Redis outage is worse than
nothing and better than an outage. For a system where the rate limit is the
*only* thing preventing abuse of an expensive resource, I would fail closed and
accept the availability hit. The deciding question is what the limiter is
protecting: here it is cost and fairness, not a security boundary.

The mitigation is that the limiter is not the only control: authentication,
per-user agent budgets and the cost alarm all still apply.

---

**"Why does a restricted experiment return 403 rather than 404? You're leaking
its existence."**

Correct, and that is deliberate. Collapsing both into 404 hides the existence
of restricted records, but it also hides genuine permission misconfiguration
from users — someone who *should* have access gets an indistinguishable "no
such thing" and files a bug about missing data instead of missing access.

Here, the *existence* of an experiment is not itself sensitive; its contents
are. In a system where existence were sensitive — say, a legal hold or an
undisclosed acquisition — I would flip it to 404 and accept the support cost.

Note that documents in *listings* and *retrieval* are filtered silently, not
403'd. You only get a 403 by directly requesting a specific id, which means you
already knew it existed.

---

**"If the model is compromised, what's the worst it can do?"**

Cause tool calls the authenticated user was already allowed to make. That is
the property I would state, and it is worth walking through why.

The model's output reaches the system as an `AgentPlan`. `select_tools` drops
anything not in the allowlist and anything the principal lacks permission for.
`ToolRegistry.call` re-validates arguments against the tool's Pydantic model.
Every query filters rows by the principal's clearance in SQL. The only write
needs an explicit human approval from a *different* person, plus a permission
the requester must independently hold.

So the blast radius is: reads the user could have done anyway, at the cost of
some tokens, all of it in `tool_audit_logs`.

What it could still do is produce a *wrong answer* — a compromised model can
lie about what the evidence says. The verifier catches ungrounded claims and
fabricated citations, but a plausible misreading of a real passage would get
through. That is a real residual risk and I would not pretend otherwise.

---

**"Your MCP server uses a static bearer token. Isn't that weak?"**

Yes, and it is the piece I would replace first in a real deployment. The MCP
specification has an OAuth2 authorisation profile, and the right answer is an
access token from the same issuer as the REST API, validated the same way.

What I did instead, because standing up an identity provider would have made
the project unrunnable: a constant-time bearer comparison, DNS-rebinding
protection left on with hosts allowlisted, and — the part that actually limits
the damage — a **restricted service principal**. The MCP identity cannot read
restricted documents or restricted experiments and has no sensitive-tool
permission. A leaked token gets you the public and internal corpus.

In AWS it also has no load balancer and no public route, so reaching it at all
requires already being inside the VPC.

The migration is contained: `BearerTokenTransport` and the middleware, nothing
else.

---

**"You shipped a malicious document in the sample data. Isn't that risky?"**

It is synthetic, it is clearly labelled, and it exists so the defence is
*demonstrable* rather than asserted. A prompt-injection defence you cannot
demonstrate is a claim.

It is also load-bearing in the test suite: the `security` evaluation suite and
`tests/integration/test_agent_api.py::TestPromptInjection` both use it, so if
someone refactored the prompt layer and broke the fencing, CI would fail.

---

## On evaluation

**"Your evaluation suite is 10 cases. That's tiny."**

It is, and it is the right size for what it is: a regression gate, not a
benchmark. Each case pins one behaviour I do not want to lose — the right tool
chosen, no fabricated citation, restricted material not leaked, sensitive
actions still pausing, a cheap question not triggering the full pipeline.

The number that matters is not the case count but whether it catches real
regressions, and it has: it found the tool-caching bug that dropped evidence on
a cache hit.

How it should grow is the more interesting answer: from **production**. An
agent run a reviewer marks as wrong becomes a regression case. A suite
consisting only of cases someone imagined up front tests imagination, not
reality — which is why `evaluation_cases` is a database table and not only a
JSON file.

---

**"You weight deterministic checks at 0.7 and the judge at 0.3. Where did those
numbers come from?"**

Judgement, not measurement — and I would say so rather than invent a
justification. What matters more than the weights is that **the weights do not
decide pass/fail**. A case fails if any deterministic check fails, full stop.
The blended score is for ranking and for seeing trends.

If I were defending the weights themselves I would need judge-versus-human
agreement data, which is exactly what `evaluation_results.human_verdict`
collects. Until there is enough of it, the honest position is that the
deterministic checks are the gate and the judge is colour.

---

**"How do you know your judge isn't just agreeing with your agent?"**

I do not, entirely, and that is the standard criticism of LLM-as-a-judge. What
I do about it:

The judge is a *different call* with an adversarial prompt and no visibility
into the agent's reasoning — it sees the question, the expected behaviour, the
evidence, the tools used and the answer. Its reasoning field comes first in the
schema, because with ordered JSON generation making the model justify before
scoring produces better-calibrated scores.

But the real answer is that it does not have to be trustworthy, because it does
not gate anything. CI runs `--no-judge`. And the human-verdict column exists
precisely to measure judge-human agreement over time; a rising disagreement
rate is the signal that the judge drifted.

---

## On cost and scale

**"What's the actual cost per query?"**

With the offline provider, zero — which is why it is the default for tests and
demos. With GPT models, it depends on the path: a small-talk question is one
fast-model call; a literature question with one retrieval is roughly a fast
call plus two reasoning calls (synthesis and verification) over ~8 evidence
passages.

The number I would rather give you is that it is *measured*: every run records
prompt tokens, completion tokens and estimated cost, and there is a CloudWatch
alarm on hourly spend. I would not quote a figure from memory because it moves
with model pricing and with question mix — what matters is that the system
tells you, per run, in the API response.

---

**"Verification doubles your LLM calls. Is it worth it?"**

For this domain, yes — and it is the kind of thing I would want measured rather
than assumed.

The cheap half is free: the deterministic citation check is set arithmetic and
costs nothing, and it catches the worst failure (a fabricated citation). The
expensive half is the LLM grounding check, which catches claims that cite a
real passage but are not supported by it.

For a research assistant where someone may act on a claimed finding, one extra
reasoning call is cheap insurance. For a low-stakes internal search box I would
run only the deterministic check and skip the model call — which is a config
change, since the verifier already degrades to deterministic-only when the
model is unavailable.

---

**"Millions of documents — where does this break first?"**

The HNSW index, once it stops fitting in memory. Recall degrades and latency
becomes bimodal.

In order: partition `document_chunks` by research area or tenant so each index
stays a workable size; add a cheap lexical pre-filter so the vector search runs
over a narrowed candidate set; tune `hnsw.ef_search` per query class rather
than globally; move embedding to a bulk pipeline with its own rate budget.

The second thing to break is connection count — every ECS task holds a pool, so
scaling tasks multiplies connections against RDS. That wants pgbouncer, not a
bigger instance.

Only after all of that would I evaluate a dedicated vector store, because
moving off pgvector costs me the single-`WHERE`-clause access filter, and that
filter is load-bearing for the security model.

---

## On the offline provider

**"Isn't the fake LLM just hiding that this doesn't work with a real model?"**

It would be, if the tests only ran against it — so that is worth addressing
directly.

There is a `tests/live/` suite that runs against the real API and checks the
things only a real model can: that structured output conforms to the Pydantic
schemas, that the grounding instruction actually produces a refusal when there
is no evidence, that injected instructions inside evidence are not obeyed, and
that embeddings relate "neoplasm" to "cancer" (which the offline provider
cannot do — it is lexical).

The division of labour is deliberate: deterministic tests assert **control
flow** — which tools ran, whether authorisation held, whether budgets bound —
and those assertions are identical whichever provider is behind the interface.
Live tests assert **model behaviour**.

And the offline provider earns its place beyond testing: it makes the demo run
with no credentials, and it is a hard floor for evaluation. A model that cannot
beat extractive quoting is not earning its cost.

---

**"Your offline embeddings are hashed bag-of-words. Doesn't that make your RAG
demo meaningless?"**

It makes it *lexical* rather than semantic, which I would state up front rather
than let someone discover. Unigrams plus bigrams, hashed into the configured
dimension with a signed contribution, L2-normalised — so cosine similarity
tracks real lexical overlap and pgvector returns genuinely relevant chunks.

What it demonstrably does: correct ranking for queries sharing vocabulary with
the corpus, unit-norm vectors, deterministic output, and the full pgvector path
(HNSW index, cosine operator, metadata filter) exercised end to end.

What it cannot do: match "neoplasm" to "cancer". That gap is exactly what a
real embedding model fills, and it is why the live test suite checks precisely
that property.

---

## On what you would change

**"What's the weakest part of this?"**

Retrieval quality measurement. I can tell you whether an answer was *grounded*
in what was retrieved, because that is a deterministic check. I cannot tell you
whether retrieval found the **best** passages, because I have no labelled
recall@k or MRR.

That matters because it is the layer where everything else is silently
constrained: if retrieval is mediocre, a perfectly grounded answer is still a
worse answer than it should be, and no test I currently have would notice.

Building it means a labelled set — question, known-relevant chunk ids — which is
work, but it is the work I would do next, before hybrid retrieval or a better
reranker, because otherwise I would be changing retrieval without being able to
prove I improved it.

---

**"If you had another week?"**

Streaming first, because time-to-first-token is what users actually feel and
the run timeline already exists to hang it on. Then retrieval metrics, for the
reason above. Then hybrid retrieval, because dense-only is weak on exactly the
identifiers researchers search for.

What I would *not* spend it on: more tools, or a second model provider. Both
are easy and neither addresses a real weakness — which is a trap worth naming,
because adding tools makes an agent demo look more impressive while making it
harder to evaluate.
