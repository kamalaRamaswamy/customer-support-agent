"""
Customer Support AI Agent — Deployment-Ready Code
=================================================
Local CLI test:
  uv run --active main.py '{"prompt": "What are the benefits of the Platinum loyalty tier?", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "What are the benefits of the Platinum loyalty tier?", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from typing import Dict

import boto3
import urllib.request
import urllib.parse
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands_tools.browser import AgentCoreBrowser
from bedrock_agentcore.tools.code_interpreter_client import code_session
from mcp.client.streamable_http import streamable_http_client
from strands import Agent, tool
from strands.hooks import (
    AfterInvocationEvent,
    HookProvider,
    HookRegistry,
)
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

# ── Logging Configuration ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)

logging.getLogger("strands").setLevel(logging.DEBUG)
logging.getLogger("bedrock_agentcore").setLevel(logging.DEBUG)

logger = logging.getLogger("CSAI_Agent")
logger.setLevel(logging.DEBUG)

# ── App Initialisation ────────────────────────────────────────────────────────
app = BedrockAgentCoreApp()
os.environ["BYPASS_TOOL_CONSENT"] = "true"

# ── Configuration ─────────────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-cxn4fmrhxn.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "DKQKGCZJBO"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-ejZEdd6ec1"
MODEL_ID    = "global.amazon.nova-2-lite-v1:0"


# ── Lazy Global Clients ───────────────────────────────────────────────────────
_bedrock_runtime = None

def get_bedrock_runtime():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
    return _bedrock_runtime


# ── Namespace Helper ──────────────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    if not memory_id or memory_id.startswith("<") or len(memory_id) < 12:
        return {}
    try:
        strategies = mem_client.get_memory_strategies(memory_id)
        return {strategy["type"]: strategy["namespaces"][0] for strategy in strategies}
    except Exception as e:
        logger.warning(f"Could not fetch memory strategies: {e}", exc_info=True)
        return {}


# ── Standalone Memory Helper ──────────────────────────────────────────────────
def retrieve_memory_context(actor_id: str, query: str, memory_client: MemoryClient, memory_id: str) -> str:
    """Retrieve existing memories directly for the prompt prior to agent creation."""
    if not memory_id or len(memory_id) < 12 or not memory_client:
        return ""
    
    namespaces = get_namespaces(memory_client, memory_id)
    if not namespaces:
        return ""

    retrieved_memories = []
    for strat_type, ns_template in namespaces.items():
        try:
            namespace = ns_template.format(actorId=actor_id, actor_id=actor_id)
            res = memory_client.retrieve_memories(
                memory_id=memory_id,
                namespace=namespace,
                query=query,
                top_k=5,
            )
            for mem in res:
                mem_text = mem.get("content", {}).get("text", "")
                if mem_text:
                    retrieved_memories.append(f"[{strat_type}] {mem_text}")
        except Exception as e:
            logger.warning(f"Error retrieving memory for {strat_type}: {e}", exc_info=True)

    if retrieved_memories:
        return "Customer Context:\n" + "\n".join(retrieved_memories) + "\n\n"
    return ""


# ── Memory Hook ───────────────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for recording support interactions post-invocation."""

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

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)

    def save_support_interaction(self, event: AfterInvocationEvent):
        if not self.memory_id or len(self.memory_id) < 12 or not self.memory_client:
            return

        agent = getattr(event, "agent", None)
        messages = getattr(agent, "messages", []) if agent else []
        if not messages:
            return

        user_query = None
        agent_response = None

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
            if "Customer Context:\n" in user_query:
                user_query = user_query.split("\n\n")[-1]

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
                logger.debug("Successfully created memory event in AgentCore Memory.")
            except Exception as e:
                logger.warning(f"Failed to create memory event: {e}", exc_info=True)


# ── Knowledge Base Tool ───────────────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.
    """
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured."

    try:
        logger.debug(f"Executing KB query: {query}")
        client = get_bedrock_runtime()
        resp = client.retrieve(
            knowledgeBaseId=KB_ID, retrievalQuery={"text": query}
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [res.get("content", {}).get("text", "") for res in results]
        return "\n---\n".join(chunks)
    except Exception as e:
        logger.error(f"Error retrieving from Knowledge Base: {e}", exc_info=True)
        return f"Error retrieving from Knowledge Base: {str(e)}"


# ── Loyalty Discount Tool ─────────────────────────────────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using Code Interpreter.
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
    "tier_discount_pct": tier_discount_rate,
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points
}}
print(json.dumps(result))
"""
    try:
        logger.debug("Executing code interpreter session for loyalty discount.")
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
        logger.warning(f"Code Interpreter execution failed: {e}. Fallback calculation applied.", exc_info=True)
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        rate = tier_rates.get(tier, 0.0)
        tier_discount = order_total * rate
        final_total = order_total - tier_discount
        return json.dumps({
            "original_subtotal": order_total,
            "tier_discount": round(tier_discount, 2),
            "tier_discount_pct": rate,
            "final_total": round(final_total, 2),
            "note": "Fallback calculation used.",
        })


# # ── Lightweight Native Web Reader Tool (Replaces Playwright / Node) ──────────
# @tool
# def fetch_web_page_content(url: str) -> str:
#     """
#     Fetch and extract clean text content from a public web page URL.
#     Use this tool whenever you need to inspect external web pages or links.
#     """
#     try:
#         logger.debug(f"Fetching content natively from URL: {url}")
#         req = urllib.request.Request(
#             url,
#             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
#         )
#         with urllib.request.urlopen(req, timeout=10) as response:
#             html = response.read().decode("utf-8", errors="ignore")
#             # Strip HTML tags & scripts to yield plain text
#             text = re.sub(r"<script.*?>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
#             text = re.sub(r"<style.*?>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
#             text = re.sub(r"<[^>]+>", " ", text)
#             clean_text = " ".join(text.split())
#             return clean_text[:4000] if clean_text else "Page loaded, but no main text content was found."
#     except Exception as e:
#         logger.error(f"Failed to fetch web content from {url}: {e}", exc_info=True)
#         return f"Unable to retrieve URL content: {str(e)}"


# ── Agent Entrypoint ──────────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for incoming requests.
    """
    try:
        logger.debug(f"Received raw incoming payload: {payload}")
        if isinstance(payload, str):
            payload = json.loads(payload)

        user_input = payload.get("prompt", "")

        if isinstance(user_input, str) and user_input.startswith("{"):
            try:
                inner_payload = json.loads(user_input)
                if isinstance(inner_payload, dict):
                    user_input = inner_payload.get("prompt", user_input)
                    payload.setdefault("customer_id", inner_payload.get("customer_id"))
                    payload.setdefault("session_id", inner_payload.get("session_id"))
            except json.JSONDecodeError:
                pass

        actor_id = payload.get("customer_id", "ANONYMOUS")
        session_id = payload.get("session_id", str(uuid.uuid4()))

        model = BedrockModel(model_id=MODEL_ID)
        memory_client = MemoryClient(region_name=REGION)

        memory_context = retrieve_memory_context(
            actor_id=actor_id,
            query=user_input,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        final_user_input = f"{memory_context}{user_input}"
        logger.debug(f"Final input string to agent: {final_user_input}")

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
            fetch_web_page_content,
            agent_core_browser.browser,
        ]

        if GATEWAY_URL and not GATEWAY_URL.startswith("<"):
            mcp_task = None
            try:
                logger.debug(f"Attempting MCP connection to: {GATEWAY_URL}")
                mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
                mcp_task = asyncio.create_task(mcp_client.load_tools())
                gateway_tools = await asyncio.wait_for(asyncio.shield(mcp_task), timeout=3.0)
                tools.extend(gateway_tools)
                logger.info("Successfully loaded MCP tools.")
            except Exception as e:
                logger.warning(f"MCP Gateway load skipped or timed out: {e}", exc_info=True)
                if mcp_task and not mcp_task.done():
                    mcp_task.cancel()
                    try:
                        await mcp_task
                    except (asyncio.CancelledError, Exception):
                        pass

        system_prompt = (
            "You are an empathetic, expert Customer Support AI Assistant for Amazon. "
            "You have access to tools for searching the knowledge base, calculating loyalty discounts, "
            "managing orders, and retrieving contents from external web pages. "
            "When asked to visit a website or check a URL, use fetch_web_page_content or agent_core_browser to retrieve the requested information."
        )

        agent = Agent(
            model=model,
            tools=tools,
            hooks=[memory_hook],
            system_prompt=system_prompt,
        )

        logger.debug("Invoking strands agent...")
        response = await agent.invoke_async(final_user_input)

        if hasattr(response, "text"):
            return response.text
        return str(response)

    except Exception as e:
        logger.error(f"Error during agent invocation: {e}", exc_info=True)
        return f"An error occurred while processing your request: {str(e)}"


# ── CLI Entry Point ───────────────────────────────────────────────────────────
def main():
    """Run one invocation from command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()

    async def _run():
        try:
            response = await invoke(json.loads(args.payload))
            print(response)
        finally:
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_run())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main()
    else:
        app.run()