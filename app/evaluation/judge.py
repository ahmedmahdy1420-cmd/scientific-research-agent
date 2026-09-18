"""LLM-as-a-judge.

What it is good for: the graded, subjective dimensions a keyword assertion
cannot express — is the answer actually relevant, is it complete given the
evidence, does it over-claim.

What it is not
--------------
It is **not** ground truth, and this project does not treat it as such:

* It is run on the reasoning model, not the model under test where that would
  be self-grading, but it is still a language model judging language.
* Known biases: it prefers longer and more confident answers, it is sensitive
  to prompt wording, and it is not reliably calibrated between runs. Absolute
  scores are weak evidence; *changes* between two runs on the same suite are
  the useful signal.
* It never decides pass/fail on its own. A case fails on the deterministic
  checks; the judge score is recorded alongside and gates only via a mean
  threshold, so a judge malfunction degrades the suite rather than silently
  passing broken behaviour.
* Every verdict is stored with its reasoning so a human can review it, and
  `evaluation_results.human_verdict` records when someone disagreed. That
  disagreement rate is how you find out the judge has drifted.
"""

from __future__ import annotations

from app.core.errors import LLMError, LLMOutputValidationError
from app.core.logging import get_logger
from app.evaluation.schemas import JudgeVerdict
from app.llm.base import LLMClient, Message, Usage
from app.llm.prompts import JUDGE_SYSTEM
from app.models.evaluation import EvaluationCase
from app.schemas.agent import AgentRunResponse

log = get_logger(__name__)

#: A case must reach this mean judge score to count as a pass, on top of
#: passing every deterministic check.
JUDGE_PASS_THRESHOLD = 0.6


class LLMJudge:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def grade(
        self, case: EvaluationCase, run: AgentRunResponse
    ) -> tuple[JudgeVerdict | None, Usage]:
        evidence_block = (
            "\n".join(
                f"[{item.source_id}] {item.title} :: {item.snippet[:400]}"
                for item in run.evidence[:12]
            )
            or "(no evidence was retrieved)"
        )

        user_content = "\n\n".join(
            [
                f"QUESTION:\n{case.question}",
                f"EXPECTED BEHAVIOUR:\n{case.expected_behavior}",
                f"EVIDENCE THE ASSISTANT HAD:\n{evidence_block}",
                f"TOOLS THE ASSISTANT USED: {[tc.tool_name for tc in run.tool_calls]}",
                f"ANSWER UNDER TEST:\n{run.answer or '(no answer produced)'}",
                f"CITATIONS CLAIMED: {run.citations}",
                "Score each criterion 0.0-1.0. Write your reasoning first.",
            ]
        )
        messages = [
            Message(role="system", content=JUDGE_SYSTEM),
            Message(role="user", content=user_content),
        ]

        try:
            result = await self._llm.structured_output(
                messages, JudgeVerdict, task="judge", temperature=0.0
            )
        except (LLMError, LLMOutputValidationError) as exc:
            # A judge failure must not fail the case: deterministic checks stand.
            log.warning("judge.failed", case=case.slug, error=exc.message)
            return None, Usage()

        verdict: JudgeVerdict = result.parsed
        log.info(
            "judge.graded",
            case=case.slug,
            mean=verdict.mean,
            grounding=verdict.grounding,
            citation_quality=verdict.citation_quality,
        )
        return verdict, result.usage
