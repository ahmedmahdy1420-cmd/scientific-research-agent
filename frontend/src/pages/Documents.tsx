import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, RequestError } from "../lib/api";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  NoticeBanner,
  PageHead,
  SkeletonRows,
  Stat,
  statusKind,
} from "../components/Common";
import { IconDocument, IconLock, IconUpload } from "../components/Icons";
import type { DocumentRecord } from "../lib/types";

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function Documents() {
  const [documents, setDocuments] = useState<DocumentRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const page = await api.listDocuments();
      setDocuments(page.items);
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "Could not load documents");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Ingestion is asynchronous, so poll while anything is still in flight.
  useEffect(() => {
    const pending = documents?.some(
      (d) => !["completed", "failed"].includes(d.ingestion_status),
    );
    if (!pending) return;
    const timer = setInterval(() => void load(), 2000);
    return () => clearInterval(timer);
  }, [documents, load]);

  const totals = useMemo(() => {
    if (!documents) return null;
    return {
      count: documents.length,
      chunks: documents.reduce((sum, doc) => sum + doc.chunk_count, 0),
      pages: documents.reduce((sum, doc) => sum + doc.page_count, 0),
      restricted: documents.filter((doc) => doc.access_level === "restricted").length,
    };
  }, [documents]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    const file = fileInput.current?.files?.[0];
    if (!file) return;
    setUploading(true);
    setError(null);
    setMessage(null);
    try {
      const result = await api.uploadDocument(file, { access_level: "internal" });
      setMessage(result.message);
      await load();
    } catch (err) {
      setError(err instanceof RequestError ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      setSelected(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function take(files: FileList | null | undefined) {
    const file = files?.[0];
    if (!file || !fileInput.current) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    fileInput.current.files = transfer.files;
    setSelected(file.name);
  }

  return (
    <>
      <PageHead
        title="Documents"
        subtitle="Uploads return immediately with 202; extraction, chunking and embedding run in a Celery worker. The list is filtered in SQL by your clearance — restricted material you cannot read is never fetched at all."
      />

      <ErrorBanner message={error} />
      {message && <NoticeBanner message={message} />}

      {totals && (
        <div className="stats summary fade-up" style={{ marginBottom: 16 }}>
          <Stat accent label="Visible to you" value={totals.count} />
          <Stat label="Pages" value={totals.pages} />
          <Stat label="Embedded chunks" value={totals.chunks} />
          <Stat label="Restricted" value={totals.restricted} />
        </div>
      )}

      <form onSubmit={upload}>
        <Card title="Upload a PDF" icon={<IconUpload size={14} />}>
          <div
            className={`dropzone${dragging ? " over" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              take(e.dataTransfer.files);
            }}
          >
            <div className="glyph" style={{ color: "var(--accent)" }}>
              <IconUpload size={20} />
            </div>
            <strong>{selected ?? "Drop a PDF here"}</strong>
            <span className="muted tiny">
              or choose a file — it is validated by magic bytes, hashed, stored in S3 and handed to
              the worker
            </span>
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              onChange={(e) => setSelected(e.target.files?.[0]?.name ?? null)}
            />
            <button
              type="button"
              className="secondary small"
              style={{ marginTop: 8 }}
              onClick={() => fileInput.current?.click()}
            >
              Choose a file
            </button>
          </div>
          <div className="row" style={{ marginTop: 14 }}>
            <button type="submit" disabled={uploading}>
              {uploading ? <span className="spinner on-accent" /> : <IconUpload size={15} />}
              {uploading ? "Uploading…" : "Upload"}
            </button>
            <span className="faint tiny">ingested as access level “internal”</span>
          </div>
        </Card>
      </form>

      {!documents && !error && (
        <Card title="Loading" icon={<IconDocument size={14} />}>
          <SkeletonRows rows={5} />
        </Card>
      )}

      {documents && documents.length === 0 && (
        <Card>
          <EmptyState
            icon={<IconDocument size={20} />}
            title="No documents you can read"
            hint="Seed the corpus, or sign in as an account with a higher clearance."
          />
        </Card>
      )}

      {documents && documents.length > 0 && (
        <Card flush title={`Corpus (${documents.length})`} icon={<IconDocument size={14} />}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Type</th>
                  <th>Access</th>
                  <th>Area</th>
                  <th>Status</th>
                  <th>Pages</th>
                  <th>Chunks</th>
                  <th>Size</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr key={doc.id}>
                    <td style={{ maxWidth: 340 }}>
                      <div style={{ fontWeight: 500 }}>
                        {doc.title.length > 66 ? `${doc.title.slice(0, 66)}…` : doc.title}
                      </div>
                      {doc.journal && <div className="faint tiny">{doc.journal}</div>}
                      {doc.ingestion_error && (
                        <div className="tiny" style={{ color: "var(--bad)" }}>
                          {doc.ingestion_error.slice(0, 90)}
                        </div>
                      )}
                    </td>
                    <td className="muted tiny">{doc.document_type.replace(/_/g, " ")}</td>
                    <td>
                      <Badge kind={doc.access_level === "restricted" ? "warn" : "info"}>
                        {doc.access_level === "restricted" && <IconLock size={10} />}
                        {doc.access_level}
                      </Badge>
                    </td>
                    <td className="muted tiny">{doc.research_area ?? "—"}</td>
                    <td>
                      <Badge kind={statusKind(doc.ingestion_status)}>
                        {doc.ingestion_status}
                      </Badge>
                    </td>
                    <td className="num">{doc.page_count}</td>
                    <td className="num">{doc.chunk_count}</td>
                    <td className="num muted tiny">{humanSize(doc.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
