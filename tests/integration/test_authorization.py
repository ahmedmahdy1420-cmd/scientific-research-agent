"""Access control end to end, against a real database.

The claim under test: a document or experiment the caller may not read is
filtered out *in SQL*, so it never enters a prompt and cannot be extracted by
any amount of prompt manipulation.
"""

from __future__ import annotations

import pytest

from app.models.enums import RoleName
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

RESTRICTED_TITLE = "Confidential internal safety review"
RESTRICTED_PHRASE = "three unpublished"


class TestDocumentVisibility:
    async def test_researcher_cannot_list_restricted_documents(
        self, api_client, auth_headers, research_corpus
    ):
        body = (
            await api_client.get(
                "/api/v1/documents?limit=100", headers=auth_headers[RoleName.RESEARCHER]
            )
        ).json()
        titles = [d["title"] for d in body["items"]]
        assert RESTRICTED_TITLE not in titles
        assert "restricted" not in {d["access_level"] for d in body["items"]}

    async def test_senior_researcher_can_list_restricted_documents(
        self, api_client, auth_headers, research_corpus
    ):
        body = (
            await api_client.get(
                "/api/v1/documents?limit=100",
                headers=auth_headers[RoleName.SENIOR_RESEARCHER],
            )
        ).json()
        assert RESTRICTED_TITLE in [d["title"] for d in body["items"]]

    async def test_direct_fetch_of_a_restricted_document_is_403(
        self, api_client, auth_headers, research_corpus
    ):
        document_id = research_corpus[RESTRICTED_TITLE]
        response = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=auth_headers[RoleName.RESEARCHER]
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "resource_access_denied"

    async def test_senior_can_fetch_the_restricted_document(
        self, api_client, auth_headers, research_corpus
    ):
        document_id = research_corpus[RESTRICTED_TITLE]
        response = await api_client.get(
            f"/api/v1/documents/{document_id}",
            headers=auth_headers[RoleName.SENIOR_RESEARCHER],
        )
        assert response.status_code == 200


class TestRetrievalFiltering:
    async def test_restricted_content_never_appears_in_researcher_retrieval(
        self, api_client, auth_headers, research_corpus
    ):
        """The core claim. Query text aimed squarely at the restricted document."""
        response = await api_client.post(
            "/api/v1/search/literature",
            headers=auth_headers[RoleName.RESEARCHER],
            json={
                "query": "internal safety review serious adverse events breast cancer",
                "limit": 50,
            },
        )
        assert response.status_code == 200
        body = response.json()
        blob = " ".join(r["content"] + r["document_title"] for r in body["results"])
        assert RESTRICTED_PHRASE not in blob
        assert RESTRICTED_TITLE not in blob
        assert body["filters_applied"]["access_levels"] == ["public", "internal"]

    async def test_senior_retrieval_does_include_restricted_content(
        self, api_client, auth_headers, research_corpus
    ):
        body = (
            await api_client.post(
                "/api/v1/search/literature",
                headers=auth_headers[RoleName.SENIOR_RESEARCHER],
                json={"query": "internal safety review serious adverse events", "limit": 50},
            )
        ).json()
        blob = " ".join(r["content"] for r in body["results"])
        assert RESTRICTED_PHRASE in blob

    async def test_agent_answer_cannot_cite_restricted_material(
        self, api_client, auth_headers, research_corpus
    ):
        """Even asking the agent directly, restricted text must not surface."""
        body = (
            await api_client.post(
                "/api/v1/chat",
                headers=auth_headers[RoleName.RESEARCHER],
                json={"question": "Summarise the internal safety review of adverse events"},
            )
        ).json()
        evidence_blob = " ".join(f"{e['title']} {e['snippet']}" for e in body.get("evidence", []))
        assert RESTRICTED_PHRASE not in evidence_blob
        assert RESTRICTED_PHRASE not in (body.get("answer") or "")


class TestExperimentVisibility:
    async def test_researcher_denied_restricted_experiment(
        self, api_client, auth_headers, research_corpus
    ):
        response = await api_client.get(
            "/api/v1/experiments/EXP-9999", headers=auth_headers[RoleName.RESEARCHER]
        )
        assert response.status_code == 403

    async def test_senior_allowed_restricted_experiment(
        self, api_client, auth_headers, research_corpus
    ):
        response = await api_client.get(
            "/api/v1/experiments/EXP-9999",
            headers=auth_headers[RoleName.SENIOR_RESEARCHER],
        )
        assert response.status_code == 200

    async def test_visible_experiment_readable_by_all_roles(
        self, api_client, auth_headers, research_corpus
    ):
        for role in (RoleName.RESEARCHER, RoleName.SENIOR_RESEARCHER, RoleName.ADMIN):
            response = await api_client.get(
                "/api/v1/experiments/EXP-0001", headers=auth_headers[role]
            )
            assert response.status_code == 200, role
            assert response.json()["results"][0]["metric_name"] == "IC50"

    async def test_unknown_experiment_is_404_not_403(
        self, api_client, auth_headers, research_corpus
    ):
        response = await api_client.get(
            "/api/v1/experiments/EXP-DOES-NOT-EXIST",
            headers=auth_headers[RoleName.ADMIN],
        )
        assert response.status_code == 404


class TestApprovalPermissions:
    async def test_researcher_cannot_reach_the_approval_endpoint(
        self, api_client, auth_headers, research_corpus
    ):
        import uuid

        response = await api_client.post(
            f"/api/v1/agent/runs/{uuid.uuid4()}/approve",
            headers=auth_headers[RoleName.RESEARCHER],
            json={"approved": True},
        )
        # 403 from the permission dependency, before the run is even looked up.
        assert response.status_code == 403
        assert response.json()["error"]["details"]["required"] == ["approvals:decide"]


class TestUploadAuthorization:
    async def test_cannot_upload_above_your_own_clearance(
        self, api_client, auth_headers, research_corpus
    ):
        """A researcher must not be able to create a restricted document they
        would then be unable to read."""
        pdf = b"%PDF-1.4\n" + b"x" * 200
        response = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("t.pdf", pdf, "application/pdf")},
            data={"access_level": "restricted"},
        )
        assert response.status_code == 403
