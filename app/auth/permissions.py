"""Roles, permissions and resource-level access rules.

Design rule that the whole project hangs on:

    **The LLM is never consulted about authorisation.**

The model may *request* `get_experiment(id=...)`. Whether that call runs is
decided here, in ordinary Python, against the authenticated principal — before
the tool executes and regardless of what any prompt or retrieved document says.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import AccessLevel, RoleName


class Permission:
    """Flat permission strings stored on the Role row."""

    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_UPLOAD = "documents:upload"
    DOCUMENTS_DELETE = "documents:delete"
    DOCUMENTS_READ_RESTRICTED = "documents:read_restricted"

    SEARCH_LITERATURE = "search:literature"
    SEARCH_TRIALS = "search:trials"

    EXPERIMENTS_READ = "experiments:read"
    EXPERIMENTS_READ_RESTRICTED = "experiments:read_restricted"
    COMPOUNDS_READ = "compounds:read"

    AGENT_RUN = "agent:run"
    AGENT_READ_ANY = "agent:read_any"

    TOOLS_SENSITIVE = "tools:sensitive"
    APPROVALS_DECIDE = "approvals:decide"

    EVALUATION_READ = "evaluation:read"
    EVALUATION_RUN = "evaluation:run"

    USERS_MANAGE = "users:manage"


RESEARCHER_PERMISSIONS: list[str] = [
    Permission.DOCUMENTS_READ,
    Permission.DOCUMENTS_UPLOAD,
    Permission.SEARCH_LITERATURE,
    Permission.SEARCH_TRIALS,
    Permission.EXPERIMENTS_READ,
    Permission.COMPOUNDS_READ,
    Permission.AGENT_RUN,
    Permission.EVALUATION_READ,
]

SENIOR_RESEARCHER_PERMISSIONS: list[str] = [
    *RESEARCHER_PERMISSIONS,
    Permission.DOCUMENTS_READ_RESTRICTED,
    Permission.EXPERIMENTS_READ_RESTRICTED,
    Permission.DOCUMENTS_DELETE,
    Permission.APPROVALS_DECIDE,
    Permission.EVALUATION_RUN,
]

ADMIN_PERMISSIONS: list[str] = [
    *SENIOR_RESEARCHER_PERMISSIONS,
    Permission.TOOLS_SENSITIVE,
    Permission.AGENT_READ_ANY,
    Permission.USERS_MANAGE,
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    RoleName.RESEARCHER: RESEARCHER_PERMISSIONS,
    RoleName.SENIOR_RESEARCHER: SENIOR_RESEARCHER_PERMISSIONS,
    RoleName.ADMIN: ADMIN_PERMISSIONS,
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    RoleName.RESEARCHER: "Runs the assistant over public and internal material.",
    RoleName.SENIOR_RESEARCHER: "Also reads restricted material and approves sensitive actions.",
    RoleName.ADMIN: "Full access, including sensitive tools and user management.",
}

#: Ordering used for "can this principal see a row at this level?" comparisons.
_ACCESS_ORDER: dict[AccessLevel, int] = {
    AccessLevel.PUBLIC: 0,
    AccessLevel.INTERNAL: 1,
    AccessLevel.RESTRICTED: 2,
}


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, resolved once per request.

    A frozen dataclass rather than the ORM object: it is cheap to pass into
    tools, threads and Celery payloads, and it cannot accidentally lazy-load or
    mutate database state deep inside the agent.
    """

    user_id: str
    email: str
    full_name: str
    roles: frozenset[str]
    permissions: frozenset[str]
    department: str | None = None

    # -- checks --------------------------------------------------------------
    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_any_permission(self, *permissions: str) -> bool:
        return any(p in self.permissions for p in permissions)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_admin(self) -> bool:
        return RoleName.ADMIN in self.roles

    @property
    def max_access_level(self) -> AccessLevel:
        """Highest document sensitivity this principal may read."""
        if self.has_permission(Permission.DOCUMENTS_READ_RESTRICTED):
            return AccessLevel.RESTRICTED
        if self.has_permission(Permission.DOCUMENTS_READ):
            return AccessLevel.INTERNAL
        return AccessLevel.PUBLIC

    @property
    def allowed_access_levels(self) -> list[AccessLevel]:
        """Levels to pass as a SQL `IN (...)` filter on every retrieval query."""
        ceiling = _ACCESS_ORDER[self.max_access_level]
        return [lvl for lvl, rank in _ACCESS_ORDER.items() if rank <= ceiling]

    def can_read_access_level(self, level: AccessLevel | str) -> bool:
        lvl = AccessLevel(level)
        return _ACCESS_ORDER[lvl] <= _ACCESS_ORDER[self.max_access_level]

    def can_read_experiment_level(self, level: AccessLevel | str) -> bool:
        lvl = AccessLevel(level)
        if lvl == AccessLevel.RESTRICTED:
            return self.has_permission(Permission.EXPERIMENTS_READ_RESTRICTED)
        return self.has_permission(Permission.EXPERIMENTS_READ)

    def to_log_fields(self) -> dict[str, str]:
        return {"user_id": self.user_id, "roles": ",".join(sorted(self.roles))}


def owns_or_can_read_any(principal: Principal, owner_id: str | None) -> bool:
    """Resource-level check for per-user objects such as agent runs."""
    if principal.has_permission(Permission.AGENT_READ_ANY):
        return True
    return owner_id is not None and owner_id == principal.user_id


#: Tools that can never be executed on the model's say-so alone.
SENSITIVE_TOOLS: frozenset[str] = frozenset({"request_sensitive_action"})

#: Permission required to *even request* a given tool. Tools absent from this
#: map are readable-by-default but still subject to the principal's filters.
TOOL_PERMISSIONS: dict[str, str] = {
    "retrieve_documents": Permission.DOCUMENTS_READ,
    "search_literature": Permission.SEARCH_LITERATURE,
    "search_clinical_trials": Permission.SEARCH_TRIALS,
    "get_experiment": Permission.EXPERIMENTS_READ,
    "get_experiment_results": Permission.EXPERIMENTS_READ,
    "search_experiments_by_compound": Permission.EXPERIMENTS_READ,
    "search_compounds": Permission.COMPOUNDS_READ,
    "mcp_search_research_data": Permission.EXPERIMENTS_READ,
    "mcp_get_trial_information": Permission.SEARCH_TRIALS,
    "request_sensitive_action": Permission.AGENT_RUN,
}
