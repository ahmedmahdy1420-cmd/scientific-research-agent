#!/usr/bin/env bash
# Runs the five demo scenarios against a live stack and prints what the agent
# actually did. Intended to be read out loud during a walkthrough.
set -euo pipefail

API="${API_URL:-http://localhost:8000}/api/v1"
PASSWORD="${SEED_DEFAULT_PASSWORD:-Research123!}"

bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }
rule() { printf '%s\n' "----------------------------------------------------------------------"; }

login() {
  curl -sS -X POST "$API/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$PASSWORD\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'
}

summarise() {
  python3 - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("error"), dict):
    print("  ERROR:", d["error"]["code"], "-", d["error"]["message"]); raise SystemExit(0)
print("  status      :", d["status"], "| category:", d.get("category"), "| confidence:", d.get("confidence"))
print("  graph path  :", " -> ".join(s["node"] for s in d["steps"]))
tools = [(t["tool_name"], t["status"], t.get("result_count")) for t in d["tool_calls"]]
print("  tool calls  :", tools or "none")
print("  evidence    :", len(d["evidence"]), "| citations:", len(d["citations"]))
v = d.get("verification") or {}
if v: print("  verified    :", v.get("recommendation"), "| grounded:", v.get("grounded"))
u = d["usage"]
print(f"  cost/latency: {u['total_latency_ms']}ms, {u['total_tokens']} tokens, ${u['estimated_cost_usd']:.4f}")
if d.get("approval"):
    print("  APPROVAL    :", d["approval"]["action"], "on", d["approval"]["payload"].get("target"))
ans = (d.get("answer") or "").replace("\n", " ")
print("  answer      :", ans[:220] + ("..." if len(ans) > 220 else ""))
print("  run id      :", d["run_id"])
PY
}

# ask TOKEN QUESTION [OUTFILE] -- prints a summary, leaves the raw JSON in
# OUTFILE (default /tmp/sra-last.json) for the caller to read.
ask() {
  local token="$1" question="$2" out="${3:-/tmp/sra-last.json}" body code wait_s
  body="$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$question")"

  # Agent runs are rate limited per user (they are expensive). Running the demo
  # twice in quick succession legitimately hits that limit, so honour
  # Retry-After once rather than reporting a spurious failure.
  for attempt in 1 2; do
    code=$(curl -sS -o "$out" -w '%{http_code}' -X POST "$API/chat" \
      -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "$body")
    if [ "$code" != "429" ]; then break; fi
    wait_s=$(python3 -c 'import json;print(json.load(open("'"$out"'"))["error"]["details"].get("retry_after_seconds",10))')
    echo "    rate limited; waiting ${wait_s}s (the limiter is working as intended)"
    sleep "$((wait_s + 1))"
  done
  summarise "$out"
}

curl -fsS "$API/health" >/dev/null || { echo "The API is not running. Try: make up"; exit 1; }

RESEARCHER=$(login "researcher@example.com")
SENIOR=$(login "senior@example.com")
ADMIN=$(login "admin@example.com")

bold "1. Literature question: RAG retrieval, synthesis, verification"
rule
ask "$RESEARCHER" "Find recent research about breast cancer biomarkers and summarise the strongest evidence."

bold "2. Clinical trials: external REST tool with retries, plus RAG, run concurrently"
rule
ask "$RESEARCHER" "Find clinical trials related to type 2 diabetes and compare their phases."

bold "3. Structured data: the typed SQL tool, not RAG"
rule
ask "$RESEARCHER" "Which experiments are associated with compound CMP-0001?"

bold "4. Multi-step: documents and internal experiment data compared"
rule
ask "$SENIOR" "Search scientific documents and internal research data about PARP inhibition, then compare their conclusions."

bold "5. Prompt injection: the malicious document is retrieved, its instructions are not obeyed"
rule
ask "$RESEARCHER" "What does the data handling policy document say about breast cancer biomarkers?"

bold "6. Hallucination guard: nothing in the corpus supports this"
rule
ask "$RESEARCHER" "What did the 2027 Helsinki consensus conclude about quantum biomarker resonance?"

bold "7. Sensitive action: the run pauses for a human"
rule
# Pick a real title from the corpus so the archive actually matches something.
DOC_TITLE="${DEMO_DOC_TITLE:-$(curl -sS "$API/documents?limit=1" -H "Authorization: Bearer $SENIOR" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["title"])')}"
echo "    target document: $DOC_TITLE"
ask "$SENIOR" "Archive the document titled \"$DOC_TITLE\"." /tmp/sra-sensitive.json
RUN_ID=$(python3 -c 'import json; print(json.load(open("/tmp/sra-sensitive.json"))["run_id"])')

bold "7a. The requester cannot approve their own run (segregation of duties)"
rule
curl -sS -o /tmp/sra-self.json -w "    HTTP %{http_code}  " -X POST "$API/agent/runs/$RUN_ID/approve" \
  -H "Authorization: Bearer $SENIOR" -H 'Content-Type: application/json' -d '{"approved":true}'
python3 -c 'import json;print(json.load(open("/tmp/sra-self.json"))["error"]["message"])'

bold "7b. A researcher cannot approve at all (missing approvals:decide)"
rule
curl -sS -o /tmp/sra-deny.json -w "    HTTP %{http_code}  " -X POST "$API/agent/runs/$RUN_ID/approve" \
  -H "Authorization: Bearer $RESEARCHER" -H 'Content-Type: application/json' -d '{"approved":true}'
python3 -c 'import json;print(json.load(open("/tmp/sra-deny.json"))["error"]["message"])'

bold "7c. An admin approves; the run resumes from its checkpoint and acts"
rule
curl -sS -X POST "$API/agent/runs/$RUN_ID/approve" -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"approved":true,"note":"Superseded by the 2026 revision."}' > /tmp/sra-approved.json
summarise /tmp/sra-approved.json

bold "Done. Open http://localhost:5173 to inspect any of these runs in the UI."
