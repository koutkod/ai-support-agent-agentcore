"""
Customer Support AI Agent
=========================
An e-commerce customer support agent built on Amazon Bedrock AgentCore and the
Strands SDK. It tracks orders and processes refunds through Lambda tools exposed
over MCP by an AgentCore Gateway, answers product and policy questions from a
Bedrock Knowledge Base, remembers customers across sessions via AgentCore Memory,
computes exact loyalty discounts in the Code Interpreter sandbox, and browses the
live web with the AgentCore Browser.

Run locally:
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── Section 1 — App Initialisation ────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── Section 2 — Configuration ─────────────────────────────────────────────────
# AWS resource identifiers for the infrastructure created in Part 1.
#
# NOTE: these four values are redacted placeholders. This project's Gateway and
# API Gateway use NONE authorizers, so the live endpoints are deliberately not
# published. Substitute your own resource IDs to run it.
#
# GATEWAY_URL — AgentCore Gateway MCP endpoint (CustomerSupportGateway)
# KB_ID       — Bedrock Knowledge Base ID (CustomerSupportKB)
# REGION      — AWS region hosting all of the above
# MEMORY_ID   — AgentCore Memory resource ID (CustomerSupportMemory)

GATEWAY_URL = "https://<your-gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "<your-kb-id>"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-<suffix>"


# ── Section 3 — Model and Clients ─────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── Section 4 — Namespace Helper ──────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)

    namespaces: Dict[str, str] = {}
    for strategy in strategies:
        # get_memory_strategies() normalizes the id/type fields but not the
        # namespace field — the service can return either the current
        # "namespaceTemplates" key or the legacy "namespaces" key, so check
        # both rather than assuming one.
        strategy_type = strategy.get("type") or strategy.get("memoryStrategyType")
        templates = strategy.get("namespaceTemplates") or strategy.get("namespaces") or []
        if strategy_type and templates:
            namespaces[strategy_type] = templates[0]

    return namespaces


# ── Section 5 — Memory Hook ───────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        # Resolved once per conversation turn rather than per-lookup; the
        # strategy/namespace configuration doesn't change mid-session.
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return

        content = last_message.get("content") or []
        # A plain-text user turn has a "text" block as its first entry. Tool
        # results and other content types are added as "user" role messages
        # too, so this guard is what actually excludes them.
        if not content or "text" not in content[0]:
            return

        user_query = content[0]["text"]

        memory_lines = []
        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)
            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
            except Exception as e:
                logger.warning("Memory retrieval failed for namespace %s: %s", namespace, e)
                continue

            for memory in memories:
                text = memory.get("content", {}).get("text", "")
                if text:
                    memory_lines.append(f"[{strategy_type}] {text}")

        if memory_lines:
            context_block = "\n".join(memory_lines)
            content[0]["text"] = f"Customer Context:\n{context_block}\n\n{user_query}"

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages

        customer_query = None
        agent_response = None

        # Walk backwards so the two most recent plain-text turns are found
        # first, skipping over any tool-use/tool-result messages in between.
        for message in reversed(messages):
            content = message.get("content") or []
            if not content or "text" not in content[0]:
                continue
            text = content[0]["text"]
            role = message.get("role")
            if role == "assistant" and agent_response is None:
                agent_response = text
            elif role == "user" and customer_query is None:
                customer_query = text
            if customer_query is not None and agent_response is not None:
                break

        if customer_query is None or agent_response is None:
            # Nothing usable to save (e.g. the turn errored before any
            # assistant text was produced).
            return

        try:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
            )
        except Exception as e:
            logger.warning("Failed to save support interaction to memory: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── Section 6 — Knowledge Base Tool ──────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured. Set KB_ID in main.py to your Knowledge Base ID."

    resp = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
    )

    results = resp.get("retrievalResults", [])
    if not results:
        return "No relevant information found in the knowledge base for that query."

    chunks = [r["content"]["text"] for r in results if r.get("content", {}).get("text")]
    return "\n---\n".join(chunks)


# ── Section 7 — Loyalty Discount Tool (Code Interpreter) ─────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # Business rules, self-contained so the sandbox has no dependency on
    # anything outside this string:
    #   - 100 points = $1 of discount, redeemed in blocks of 500 points
    #   - points redemption is capped at 50% of the order total
    #   - the tier discount applies to the subtotal *after* the points
    #     discount, matching how the tier benefits are described in the
    #     product catalog ("15% discount" on top of whatever else applies)
    #   - points are earned on the *category-weighted* order total and
    #     folded into the reported remaining balance
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = {tier!r}
order_total = {order_total}
product_category = {product_category!r}

max_redeemable_value = order_total * 0.5
max_points_for_cap = int(max_redeemable_value * 100)
usable_points = min(loyalty_points, max_points_for_cap)
points_redeemed = (usable_points // 500) * 500
if points_redeemed < 500:
    points_redeemed = 0

points_discount_value = points_redeemed / 100
tier_discount_pct = tier_rates.get(tier, 0.0)
subtotal_after_points = order_total - points_discount_value
tier_discount_value = round(subtotal_after_points * tier_discount_pct, 2)
final_total = round(subtotal_after_points - tier_discount_value, 2)
total_savings = round(order_total - final_total, 2)
points_earned = int(order_total * earn_rates.get(product_category, 1))
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "points_redeemed": points_redeemed,
    "points_discount_value": points_discount_value,
    "tier_discount_pct": tier_discount_pct,
    "tier_discount_value": tier_discount_value,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in response["stream"]:
                result_event = event.get("result", {})
                for block in result_event.get("content", []):
                    if "text" in block:
                        return block["text"]
                # No text content block found in the first result event --
                # fall back to returning the raw event so nothing is lost.
                return json.dumps(result_event)
            return json.dumps({"error": "Code interpreter returned no result."})

    except Exception as e:
        logger.warning(
            "Code Interpreter unavailable (%s); falling back to a tier-only discount.", e
        )
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_discount_pct = tier_rates.get(tier, 0.0)
        tier_discount_value = round(order_total * tier_discount_pct, 2)
        final_total = round(order_total - tier_discount_value, 2)
        return json.dumps({
            "points_redeemed": 0,
            "tier_discount_pct": tier_discount_pct,
            "final_total": final_total,
            "remaining_points": loyalty_points,
            "note": "Code Interpreter unavailable; points were not redeemed, only the tier discount was applied.",
        })


# ── Section 8 — Agent Entrypoint ──────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a customer support assistant for an e-commerce platform. You can "
    "track orders, process refunds, answer product and policy questions using "
    "the knowledge base tool, calculate exact loyalty discounts using the "
    "loyalty discount tool, and browse the web for live information using the "
    "browser tool. Always use the tools available to you rather than guessing "
    "at facts like order status, policy details, or discount math. Be concise "
    "and professional."
)


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id") or "anonymous"
        session_id = payload.get("session_id") or str(uuid.uuid4())

        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        gateway_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))

        # Keep the Gateway connection open for the whole agent invocation --
        # not just while listing tools -- since the MCPAgentTool objects
        # need a live session to actually call the tools during the run.
        with gateway_client:
            gateway_tools = gateway_client.list_tools_sync()
            all_tools = tools + list(gateway_tools)

            agent = Agent(
                model=model,
                tools=all_tools,
                system_prompt=SYSTEM_PROMPT,
                hooks=[memory_hook],
            )

            result = await agent.invoke_async(user_input)

        content = result.message.get("content", [])
        if content and "text" in content[0]:
            return content[0]["text"]
        return str(result)

    except Exception as e:
        logger.exception("Agent invocation failed")
        return f"Sorry, something went wrong while processing your request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
