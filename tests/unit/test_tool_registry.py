"""The authorisation choke point.

If any of these fail, the LLM has become a security boundary.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from app.auth.permissions import Permission
from app.core.config import get_settings
from app.database.session import get_async_session_factory
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50)
    count: int = Field(default=1, ge=1, le=5)


class EchoTool(Tool):
    name = "echo"
    description = "Echo text back"
    input_model = EchoRequest
    required_permission = Permission.DOCUMENTS_READ

    def __init__(self) -> None:
        self.calls: list[EchoRequest] = []

    async def run(self, args: EchoRequest, ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        return ToolResult(
            tool=self.name,
            ok=True,
            summary="echoed",
            data=[args.text] * args.count,
            count=args.count,
        )


class SlowTool(Tool):
    name = "slow"
    description = "Sleeps past its budget"
    input_model = EchoRequest
    required_permission = None
    timeout_seconds = 0.05

    async def run(self, args: EchoRequest, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(2)
        return ToolResult(tool=self.name, ok=True, summary="never reached")


class ExplodingTool(Tool):
    name = "explode"
    description = "Raises"
    input_model = EchoRequest
    required_permission = None

    async def run(self, args: EchoRequest, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("upstream blew up with secret=hunter2")


class SensitiveTool(Tool):
    name = "danger"
    description = "Changes state"
    input_model = EchoRequest
    required_permission = None
    sensitive = True

    def __init__(self) -> None:
        self.executed = False

    async def run(self, args: EchoRequest, ctx: ToolContext) -> ToolResult:
        self.executed = True
        return ToolResult(tool=self.name, ok=True, summary="did the thing")


@pytest.fixture
def ctx(researcher):
    return ToolContext(principal=researcher, session_factory=get_async_session_factory())


@pytest.fixture
def registry():
    return ToolRegistry(
        [EchoTool(), SlowTool(), ExplodingTool(), SensitiveTool()],
        settings=get_settings(),
        cache=None,
    )


class TestAuthorisation:
    async def test_permitted_tool_executes(self, registry, ctx):
        result = await registry.call("echo", {"text": "hi", "count": 2}, ctx)
        assert result.ok
        assert result.data == ["hi", "hi"]

    async def test_missing_permission_is_denied_and_never_runs(self, registry):
        """A principal without documents:read must not reach the tool body."""
        from app.auth.permissions import Principal

        stranger = Principal(
            user_id="nobody",
            email="n@t",
            full_name="N",
            roles=frozenset(),
            permissions=frozenset(),
        )
        tool = registry.get("echo")
        result = await registry.call(
            "echo",
            {"text": "hi"},
            ToolContext(principal=stranger, session_factory=get_async_session_factory()),
        )
        assert not result.ok
        assert result.error_code == "authorization_failed"
        assert tool.calls == []  # the body never executed

    async def test_unknown_tool_is_refused(self, registry, ctx):
        result = await registry.call("rm_rf", {"path": "/"}, ctx)
        assert not result.ok
        assert result.error_code == "tool_not_found"

    async def test_only_permitted_tools_are_advertised(self, registry, researcher, admin):
        names = {t.name for t in registry.schemas_for(researcher)}
        assert "echo" in names
        from app.auth.permissions import Principal

        stranger = Principal(
            user_id="n",
            email="n@t",
            full_name="N",
            roles=frozenset(),
            permissions=frozenset(),
        )
        assert "echo" not in {t.name for t in registry.schemas_for(stranger)}


class TestArgumentValidation:
    async def test_invalid_arguments_rejected_before_execution(self, registry, ctx):
        tool = registry.get("echo")
        result = await registry.call("echo", {"text": "", "count": 99}, ctx)
        assert not result.ok
        assert result.error_code == "tool_input_invalid"
        assert tool.calls == []

    async def test_unknown_argument_does_not_reach_the_tool(self, registry, ctx):
        result = await registry.call(
            "echo", {"text": "ok", "__proto__": "evil", "drop": "table"}, ctx
        )
        assert result.ok
        assert registry.get("echo").calls[0].text == "ok"

    async def test_missing_required_argument_rejected(self, registry, ctx):
        result = await registry.call("echo", {}, ctx)
        assert not result.ok
        assert result.error_code == "tool_input_invalid"


class TestFailureHandling:
    async def test_timeout_is_bounded_and_reported(self, registry, ctx):
        result = await registry.call("slow", {"text": "x"}, ctx)
        assert not result.ok
        assert result.error_code == "tool_timeout"

    async def test_exception_becomes_a_failed_result_not_a_crash(self, registry, ctx):
        result = await registry.call("explode", {"text": "x"}, ctx)
        assert not result.ok
        assert result.error_code == "tool_error"

    async def test_model_payload_hides_internal_detail(self, registry, ctx):
        """Error text shown to the model must not leak internals."""
        result = await registry.call("explode", {"text": "x"}, ctx)
        payload = result.to_model_payload()
        assert payload["ok"] is False
        assert payload["error"] == "tool_error"
        assert "Traceback" not in str(payload)


class TestSensitiveGate:
    async def test_sensitive_tool_blocked_without_approval(self, registry, ctx):
        tool = registry.get("danger")
        result = await registry.call("danger", {"text": "go"}, ctx)
        assert not result.ok
        assert result.error_code == "approval_required"
        assert tool.executed is False

    async def test_sensitive_tool_runs_once_approved(self, registry, researcher):
        tool = registry.get("danger")
        approved_ctx = ToolContext(
            principal=researcher,
            session_factory=get_async_session_factory(),
            approved_actions=frozenset({"danger"}),
        )
        result = await registry.call("danger", {"text": "go"}, approved_ctx)
        assert result.ok
        assert tool.executed is True

    async def test_approval_for_one_tool_does_not_unlock_another(self, registry, researcher):
        """An approval is scoped to the action that was approved."""
        registry.register(_SecondSensitiveTool())
        ctx = ToolContext(
            principal=researcher,
            session_factory=get_async_session_factory(),
            approved_actions=frozenset({"danger"}),
        )
        result = await registry.call("danger2", {"text": "go"}, ctx)
        assert not result.ok
        assert result.error_code == "approval_required"


class _SecondSensitiveTool(Tool):
    name = "danger2"
    description = "Another state change"
    input_model = EchoRequest
    required_permission = None
    sensitive = True

    async def run(self, args: EchoRequest, ctx: ToolContext) -> ToolResult:
        return ToolResult(tool=self.name, ok=True, summary="should not happen")


class TestCaching:
    """Regression cover for a bug the evaluation suite caught.

    A cacheable tool stored only its summary/data, so the second identical call
    returned a result with NO evidence. The answer then had nothing to cite and
    the verifier refused it - a failure that only appeared on the *second* run
    of the same question, which is exactly the kind of thing a unit test should
    pin down.
    """

    class _CachingEchoTool(Tool):
        name = "cached_echo"
        description = "Cacheable tool that produces evidence"
        input_model = EchoRequest
        required_permission = None
        cacheable = True

        def __init__(self) -> None:
            self.executions = 0

        async def run(self, args: EchoRequest, ctx: ToolContext) -> ToolResult:
            from app.schemas.agent import EvidenceItem

            self.executions += 1
            return ToolResult(
                tool=self.name,
                ok=True,
                summary="produced evidence",
                data=[args.text],
                evidence=[
                    EvidenceItem(
                        source_type="document",
                        source_id="doc:cached:0",
                        title="Cached source",
                        snippet="A finding worth citing.",
                    )
                ],
                count=1,
            )

    @pytest.fixture
    def memory_cache(self):
        class MemoryCache:
            def __init__(self) -> None:
                self.store: dict[str, object] = {}

            async def get(self, key: str):
                return self.store.get(key)

            async def set(self, key: str, value, ttl: int | None = None) -> None:
                import json

                # Round-trip through JSON so the test reproduces real Redis
                # serialisation rather than handing back the same objects.
                self.store[key] = json.loads(json.dumps(value, default=str))

            async def delete(self, *keys: str) -> None:
                for key in keys:
                    self.store.pop(key, None)

        return MemoryCache()

    async def test_cache_hit_still_carries_evidence(self, memory_cache, ctx):
        tool = self._CachingEchoTool()
        registry = ToolRegistry([tool], settings=get_settings(), cache=memory_cache)

        first = await registry.call("cached_echo", {"text": "hello"}, ctx)
        second = await registry.call("cached_echo", {"text": "hello"}, ctx)

        assert tool.executions == 1, "second call should have been served from cache"
        assert second.cache_hit is True
        assert second.count == first.count
        assert [e.source_id for e in second.evidence] == [e.source_id for e in first.evidence]
        assert second.evidence[0].title == "Cached source"

    async def test_different_arguments_are_cached_separately(self, memory_cache, ctx):
        tool = self._CachingEchoTool()
        registry = ToolRegistry([tool], settings=get_settings(), cache=memory_cache)
        await registry.call("cached_echo", {"text": "one"}, ctx)
        await registry.call("cached_echo", {"text": "two"}, ctx)
        assert tool.executions == 2


class TestConcurrency:
    async def test_independent_calls_run_in_parallel(self, registry, ctx):
        results = await registry.call_many(
            [("echo", {"text": "a"}), ("echo", {"text": "b"}), ("echo", {"text": "c"})],
            ctx,
        )
        assert len(results) == 3
        assert all(r.ok for r in results)
        assert {r.data[0] for r in results} == {"a", "b", "c"}

    async def test_one_failure_does_not_abort_the_others(self, registry, ctx):
        results = await registry.call_many(
            [("echo", {"text": "a"}), ("explode", {"text": "b"})], ctx
        )
        assert [r.ok for r in results] == [True, False]


class TestRegistryHygiene:
    def test_duplicate_registration_rejected(self, registry):
        with pytest.raises(ValueError, match="Duplicate"):
            registry.register(EchoTool())

    def test_real_registry_matches_the_documented_allowlist(self):
        from app.tools import build_tool_registry

        names = set(build_tool_registry().names)
        expected = {
            "retrieve_documents",
            "search_literature",
            "search_clinical_trials",
            "get_experiment",
            "get_experiment_results",
            "search_compounds",
            "search_experiments_by_compound",
            "request_sensitive_action",
        }
        assert expected <= names

    def test_access_filtered_tools_are_not_cacheable(self):
        """A shared cache entry for an access-filtered tool would leak data."""
        from app.tools import build_tool_registry

        registry = build_tool_registry()
        for name in ("retrieve_documents", "search_literature"):
            assert registry.get(name).cacheable is False, (
                f"{name} filters by access level and must not be globally cached"
            )
