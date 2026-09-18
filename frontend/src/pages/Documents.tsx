import { useCallback, useEffect, useRef, useState } from "react";
import { api, RequestError } from "../lib/api";
import { Badge, ErrorBanner, Spinner, statusKind } from "../components/Common";
import type { DocumentRecord } from "../lib/types";

export default function Documents() {
  const [documents, setDocuments] = useState<DocumentRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
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
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <>
      <h2>Documents</h2>
      <p className="subtitle">
        Uploads return immediately with 202; extraction, chunking and embedding run in a
        Celery worker. You only see documents your role is cleared to read.
      </p>

      <ErrorBanner message={error} />
      {message && <div className="notice">{message}</div>}

      <form className="card" onSubmit={upload}>
        <h3 style={{ marginTop: 0 }}>Upload a PDF</h3>
        <div className="row">
          <input ref={fileInput} type="file" accept="application/pdf" />
          <button type="submit" disabled={uploading}>
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
      </form>

      {!documents && !error && <Spinner label="Loading documents…" />}
      {documents && (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Title</th><th>Type</th><th>Access</th>
                <th>Area</th><th>Status</th><th>Pages</th><th>Chunks</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td>
                    {doc.title.slice(0, 66)}
                    {doc.ingestion_error && (
                      <div className="muted" style={{ color: "var(--bad)" }}>
                        {doc.ingestion_error.slice(0, 90)}
                      </div>
                    )}
                  </td>
                  <td className="muted">{doc.document_type}</td>
                  <td>
                    <Badge kind={doc.access_level === "restricted" ? "warn" : "info"}>
                      {doc.access_level}
                    </Badge>
                  </td>
                  <td className="muted">{doc.research_area ?? "-"}</td>
                  <td><Badge kind={statusKind(doc.ingestion_status)}>{doc.ingestion_status}</Badge></td>
                  <td>{doc.page_count}</td>
                  <td>{doc.chunk_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
