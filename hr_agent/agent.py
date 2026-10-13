"""The HR policy agent, built with LangChain.

Equivalent of the n8n `AI Agent` node, whose nodes were themselves LangChain:

    ChatOpenAI            <- `OpenAI Chat Model`
    get_hr_policy_document <- `Get a document in Google Docs` (ai_tool)
    create_agent          <- `AI Agent`
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver

from hr_agent import config
from hr_agent.policy import PolicySource, PolicyUnavailableError

# One shared reader so the document cache is reused across calls.
_policy_source = PolicySource()


def make_policy_tool(source: PolicySource):
    """Build the policy tool over a given reader.

    A closure rather than a module-level tool so evals can inject a different
    document — including a poisoned one — without touching global state.
    """

    @tool
    def get_hr_policy_document() -> str:
        """Read the company HR policy document.

        Returns the full text of the UK accountancy firm's Employee Handbook and
        HR Policy, covering recruitment, leave entitlement, conduct, and
        procedures. Call this before answering any question about company policy.
        """
        try:
            return source.read()
        except PolicyUnavailableError as exc:
            # Returned to the model rather than raised, so it tells the user it
            # cannot see the policy instead of using general knowledge.
            return f"ERROR: the HR policy document could not be read. {exc}"

    return get_hr_policy_document


get_hr_policy_document = make_policy_tool(_policy_source)


@dataclass
class AgentReply:
    """An agent turn plus the trace an eval needs to inspect it."""

    text: str
    tool_calls: list[str] = field(default_factory=list)
    # What the tools returned — the retrieved context, for eval metrics.
    tool_outputs: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text


class HRPolicyAgent:
    """A multi-turn HR policy assistant backed by the policy document."""

    def __init__(
        self,
        *,
        model: str | None = None,
        prompt_variant: str | None = None,
        temperature: float | None = None,
        policy_source: PolicySource | None = None,
    ) -> None:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set (expected in .env).")

        self.model_name = model or config.AGENT_MODEL
        llm = ChatOpenAI(
            model=self.model_name,
            temperature=config.TEMPERATURE if temperature is None else temperature,
            api_key=config.OPENAI_API_KEY,
        )

        self.agent = create_agent(
            llm,
            tools=[
                get_hr_policy_document
                if policy_source is None
                else make_policy_tool(policy_source)
            ],
            system_prompt=config.system_prompt(prompt_variant),
            # Conversation memory, keyed by thread_id below.
            checkpointer=InMemorySaver(),
        )
        self.thread_id = str(uuid.uuid4())

    def reset(self) -> None:
        """Start a new conversation. The document cache is left intact."""
        self.thread_id = str(uuid.uuid4())

    def ask(self, user_message: str) -> str:
        """Send one user turn and return the agent's text reply."""
        return self.ask_with_trace(user_message).text

    def ask_with_trace(self, user_message: str) -> AgentReply:
        """Send one user turn and return the reply plus which tools were called."""
        result = self.agent.invoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config={"configurable": {"thread_id": self.thread_id}},
        )
        messages = result["messages"]
        tool_calls = [
            call["name"]
            for message in messages
            for call in (getattr(message, "tool_calls", None) or [])
        ]

        tool_outputs = [
            str(m.content)
            for m in messages
            if getattr(m, "type", None) == "tool"
        ]

        last = messages[-1]
        text = getattr(last, "text", None)
        # langchain-core 1.x returns a TextAccessor (a str subclass that is also
        # callable); calling it is deprecated, so read it as a value.
        if isinstance(text, str):
            text = str(text)
        elif callable(text):  # older versions exposed .text() as a method only
            text = text()
        else:
            text = last.content

        return AgentReply(
            text=text or "", tool_calls=tool_calls, tool_outputs=tool_outputs
        )
