/** Mirrors the Pydantic response schemas in app/schemas/. */

export interface Role {
  id: string;
  name: string;
  description: string;
  permissions: string[];
}

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  department: string | null;
  roles: Role[];
  permissions: string[];
  max_access_level: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_at: string;
}

export interface EvidenceItem {
  source_type: "document" | "experiment" | "clinical_trial" | "compound";
  source_id: string;
  title: string;
  snippet: string;
  origin_tool: string | null;
  relevance_score: number | null;
  publication_date: string | null;
  url: string | null;
}

export interface ToolCallRecord {
  id: string;
  tool_name: string;
  status: string;
  arguments: Record<string, unknown>;
  result_summary: string | null;
  result_count: number | null;
  denial_reason: string | null;
  error_code: string | null;
  latency_ms: number;
  retry_count: number;
  cache_hit: boolean;
  created_at: string;
}

export interface AgentStep {
  node: string;
  sequence: number;
  summary: string;
  detail: Record<string, unknown>;
}

export interface PlannedStep {
  tool: string;
  rationale: string;
  arguments: Record<string, unknown>;
}

export interface AgentPlan {
  objective: string;
  steps: PlannedStep[];
  needs_human_approval: boolean;
  expected_evidence: string;
}

export interface VerificationResult {
  grounded: boolean;
  sufficient: boolean;
  unsupported_claims: string[];
  invalid_citations: string[];
  missing_information: string | null;
  recommendation: "accept" | "retrieve_more" | "answer_with_caveats" | "refuse";
  reasoning: string;
}

export interface ApprovalPrompt {
  approval_id: string;
  action: string;
  reason: string;
  risk_level: string;
  payload: Record<string, unknown>;
  expires_at: string | null;
}

export interface UsageStats {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  llm_latency_ms: number;
  tool_latency_ms: number;
  retrieval_latency_ms: number;
  total_latency_ms: number;
  iterations: number;
  tool_calls: number;
  models_used: string[];
  cache_hits: number;
}

export interface AgentRun {
  run_id: string;
  status: string;
  question: string;
  answer: string | null;
  confidence: string | null;
  category: string | null;
  evidence: EvidenceItem[];
  citations: string[];
  caveats: string[];
  plan: AgentPlan | null;
  steps: AgentStep[];
  tool_calls: ToolCallRecord[];
  verification: VerificationResult | null;
  approval: ApprovalPrompt | null;
  usage: UsageStats;
  error: string | null;
  created_at: string | null;
}

export interface AgentRunSummary {
  id: string;
  question: string;
  status: string;
  category: string | null;
  created_at: string;
  total_latency_ms: number;
  estimated_cost_usd: number;
  tool_calls_used: number;
}

export interface DocumentRecord {
  id: string;
  title: string;
  filename: string;
  document_type: string;
  access_level: string;
  authors: string[];
  journal: string | null;
  doi: string | null;
  publication_date: string | null;
  research_area: string | null;
  abstract: string | null;
  ingestion_status: string;
  ingestion_error: string | null;
  page_count: number;
  chunk_count: number;
  size_bytes: number;
  created_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvaluationResult {
  id: string;
  case_id: string;
  agent_run_id: string | null;
  run_label: string;
  passed: boolean;
  deterministic_scores: Record<string, unknown>;
  judge_scores: Record<string, number>;
  judge_reasoning: string | null;
  overall_score: number;
  failures: string[];
  latency_ms: number;
  cost_usd: number;
  tools_used: string[];
  answer: string | null;
  human_verdict: string | null;
  created_at: string;
}

export interface SuiteSummary {
  run_label: string;
  total: number;
  passed: number;
  failed: number;
  pass_rate: number;
  mean_score: number;
  mean_latency_ms: number;
  total_cost_usd: number;
  by_failure: Record<string, number>;
}

export interface EvaluationCase {
  id: string;
  slug: string;
  suite: string;
  question: string;
  expected_behavior: string;
  expected_category: string | null;
  expected_tools: string[];
  forbidden_tools: string[];
  requires_citations: boolean;
  requires_approval: boolean;
  as_role: string;
  enabled: boolean;
}

export interface ApiError {
  error: { code: string; message: string; retryable: boolean; details: Record<string, unknown> };
  request_id: string | null;
}
