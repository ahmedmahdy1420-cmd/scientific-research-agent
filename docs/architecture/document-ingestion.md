# Document ingestion

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant API as FastAPI
    participant S3 as S3 / local storage
    participant DB as PostgreSQL
    participant Q as Redis (broker)
    participant W as Celery worker
    participant E as Embedding provider

    U->>API: POST /documents (multipart PDF)
    API->>API: size check · magic-byte check · access-level check
    Note over API: Content-Type is attacker-controlled,<br/>so the bytes are what gets validated
    API->>API: sha256(bytes) → content address
    API->>DB: SELECT by content_sha256
    alt already ingested
        DB-->>API: existing document
        API-->>U: 202 { duplicate_of }
    else new
        API->>S3: PUT documents/<hash>/<name>.pdf
        API->>DB: INSERT document (status=uploaded)
        API->>Q: enqueue documents.ingest
        API-->>U: 202 { document_id, task_id }
    end

    Note over U,API: The HTTP request ends here.<br/>Everything below is background work.

    W->>Q: reserve task
    W->>DB: status = processing
    W->>S3: GET the bytes
    W->>W: PyMuPDF text extraction
    W->>W: clean (de-hyphenate, strip headers, keep paragraphs)
    W->>W: metadata (title, authors, DOI, date)
    W->>DB: status = extracted
    W->>W: chunk (~550 tokens, 80 overlap, page + section tracked)
    W->>DB: DELETE existing chunks, INSERT new ones
    W->>DB: status = chunked
    W->>E: embed all chunks (batched, concurrent)
    E-->>W: vectors
    W->>DB: UPDATE embeddings, status = embedded → completed

    U->>API: GET /documents/{id}/status
    API-->>U: { status: completed, chunks: 12 }
```

## The state machine

```
uploaded → processing → extracted → chunked → embedded → completed
                 │
                 └──────────────────────────────────────→ failed
```

Persisted on `documents.ingestion_status`, which is what makes the pipeline
restartable and the UI pollable.

## Why none of this is in the request

Extraction and embedding are seconds to minutes of CPU and network. Inline,
they would hold a worker, blow past any sane gateway timeout, and lose all
progress if the client disconnected. The request does four cheap things —
validate, hash, store, insert — and returns 202 with an id to poll.

## Idempotency, because Celery is at-least-once

`acks_late=True` means a task is only acknowledged after it completes, so a
worker killed mid-ingestion (spot reclaim, deploy, OOM) returns the task to the
queue. That is only safe because the pipeline converges on the same result when
run twice:

* the storage key is the content hash, so re-uploading identical bytes is a no-op;
* chunking deletes this document's chunks before inserting, so a retry replaces
  rather than duplicates;
* embeddings are recomputed deterministically for the chunks that exist.

## Retry policy

Only transient classes retry (`EmbeddingError`, `ExternalServiceError`,
connection and OS errors) with exponential backoff and jitter, capped at four
attempts. A `ValidationError` — a corrupt PDF, a scanned image with no text
layer — is **not** retried: repeating it just delays the failure and triples the
log noise. The document lands in `failed` with the reason recorded and visible
in the UI.
