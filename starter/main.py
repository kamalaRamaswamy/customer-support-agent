"""
Customer Support AI Agent — Completed Code
==========================================
Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import argparse
import asyncio
import json
import logging
import os
import uuid
from typing import Dict

import boto3
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import code_session
from mcp.client.streamable_http import streamable_http_client
from strands import Agent, tool
from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookProvider,
    HookRegistry,
)
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from strands_tools.browser import AgentCoreBrowser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-cxn4fmrhxn.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "IUSZMGCJVI"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-ejZEdd6ec1"


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    if not memory_id or memory_id.startswith("<") or len(memory_id) < 12:
        logger.warning("Memory ID is missing or invalid (must be at least 12 characters).")
        return {}
    try:
        strategies = mem_client.get_memory_strategies(memory_id)
        return {strategy["type"]: strategy["namespaces"][0] for strategy in strategies}
    except Exception as e:
        logger.warning(f"Could not fetch memory strategies: {e}")
        return {}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        super().__init__()
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    def register_hooks(self, registry: HookRegistry) -> None:
        """Register lifecycle event callbacks with the hook registry."""
        registry.add_callback(BeforeInvocationEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)

    def retrieve_customer_context(self, event: BeforeInvocationEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        if not self.namespaces or len(self.memory_id) < 12:
            return

        agent = getattr(event, "agent", None)
        messages = getattr(agent, "messages", []) if agent else []
        if not messages:
            return

        last_message = messages[-1]

        # Guard: Check it is a plain text user message (not tool result)
        if last_message.get("role") != "user":
            return

        content = last_message.get("content", [])
        if isinstance(content, list):
            text_blocks = [c.get("text", "") for c in content if "text" in c]
            user_query = " ".join(text_blocks)
        else:
            user_query = str(content)

        if not user_query.strip():
            return

        retrieved_memories = []
        for strat_type, ns_template in self.namespaces.items():
            try:
                namespace = ns_template.format(actorId=self.actor_id)
                res = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
                for mem in res:
                    mem_text = mem.get("content", {}).get("text", "")
                    if mem_text:
                        retrieved_memories.append(f"[{strat_type}] {mem_text}")
            except Exception as e:
                logger.warning(f"Error retrieving memory for {strat_type}: {e}")

        if retrieved_memories:
            formatted_context = "Customer Context:\n" + "\n".join(retrieved_memories)
            if isinstance(content, list) and content:
                content[0]["text"] = f"{formatted_context}\n\n{content[0].get('text', '')}"
            else:
                last_message["content"] = f"{formatted_context}\n\n{user_query}"

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        if not self.memory_id or len(self.memory_id) < 12:
            return

        agent = getattr(event, "agent", None)
        messages = getattr(agent, "messages", []) if agent else []
        if not messages:
            return

        user_query = None
        agent_response = None

        # Walk backwards to find last text user query and last assistant response
        for msg in reversed(messages):
            role = msg.get("role")
            content = msg.get("content", "")

            if isinstance(content, list):
                text_content = " ".join([c.get("text", "") for c in content if "text" in c])
            else:
                text_content = str(content)

            if role == "assistant" and not agent_response and text_content.strip():
                agent_response = text_content
            elif role == "user" and not user_query and text_content.strip():
                user_query = text_content

            if user_query and agent_response:
                break

        if user_query and agent_response:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (user_query, "USER"),
                        (agent_response, "ASSISTANT"),
                    ],
                )
            except Exception as e:
                logger.warning(f"Failed to create memory event: {e}")


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
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
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID, retrievalQuery={"text": query}
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [res.get("content", {}).get("text", "") for res in results]
        return "\n---\n".join(chunks)
    except Exception as e:
        return f"Error retrieving from Knowledge Base: {str(e)}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
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
    code = f"""
import json, math

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

points = {loyalty_points}
tier = "{tier}"
subtotal = {order_total}
category = "{product_category}"

max_points_discount = subtotal * 0.50
usable_points = min(points, math.floor(max_points_discount * 100))
points_redeemed = (usable_points // 500) * 500
points_discount = points_redeemed / 100.0

remaining_subtotal = max(0.0, subtotal - points_discount)
tier_discount_rate = tier_rates.get(tier, 0.0)
tier_discount = remaining_subtotal * tier_discount_rate

final_total = max(0.0, remaining_subtotal - tier_discount)
total_savings = subtotal - final_total
earn_rate = earn_rates.get(category, 1)
points_earned = int(final_total * earn_rate)
remaining_points = (points - points_redeemed) + points_earned

result = {{
    "original_subtotal": subtotal,
    "points_redeemed": points_redeemed,
    "points_discount": round(points_discount, 2),
    "tier_discount": round(tier_discount, 2),
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points
}}
print(json.dumps(result))
"""

    try:
        session = code_session(REGION)
        exec_result = session.invoke(
            "executeCode",
            {"code": code, "language": "python", "clearContext": True},
        )
        for event in exec_result:
            if "result" in event:
                return str(event["result"])
        return str(exec_result)
    except Exception as e:
        logger.warning(f"Code Interpreter execution failed: {e}. Executing fallback calculation.")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        rate = tier_rates.get(tier, 0.0)
        tier_discount = order_total * rate
        final_total = order_total - tier_discount
        return json.dumps({
            "original_subtotal": order_total,
            "tier_discount": round(tier_discount, 2),
            "final_total": round(final_total, 2),
            "note": "Fallback calculation used (Code Interpreter unavailable).",
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
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
        actor_id = payload.get("customer_id", "ANONYMOUS")
        session_id = payload.get("session_id", str(uuid.uuid4()))

        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        # Initialize Managed AgentCore Browser
        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
        ]
        
        # Extend tools list with AgentCore Managed Browser tools
        tools.extend(agent_core_browser.tools)

        system_prompt = (
            "You are an empathetic, expert Customer Support AI Assistant for Amazon. "
            "Use the provided knowledge base, loyalty discount calculator, browser, and order management tools "
            "to resolve customer requests accurately and efficiently."
        )

        # Append MCPClient to tools; Agent will manage start and discovery automatically
        if GATEWAY_URL and not GATEWAY_URL.startswith("<"):
            try:
                mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
                tools.append(mcp_client)
            except Exception as e:
                logger.warning(f"Failed to load MCP tools or gateway connection failed: {e}")

        agent = Agent(
            model=model,
            tools=tools,
            hooks=[memory_hook],
            system_prompt=system_prompt,
        )

        response = agent(user_input)

        if hasattr(response, "text"):
            return response.text
        return str(response)

    except Exception as e:
        logger.error(f"Error during agent invocation: {e}", exc_info=True)
        return f"An error occurred while processing your request: {str(e)}"


# ── CLI entry point ──────────────────────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    # app.run()  # Active for AgentCore deployment
    main()     # Active for local CLI testing: uv run main.py '{"prompt": "Hello"}'