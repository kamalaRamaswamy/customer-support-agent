import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

GATEWAY_URL = "https://customersupportgateway-x7ouke3ach.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

async def main():
    print(f"Connecting to Gateway: {GATEWAY_URL}...")
    async with streamable_http_client(GATEWAY_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            
            # 1. Discover registered tools
            tools_response = await session.list_tools()
            print("\n--- Discovered Gateway Tools ---")
            for tool in tools_response.tools:
                print(f"Tool Name: {tool.name}")
                print(f"Description: {tool.description}\n")

            # 2. Test Order Tracker (replace tool name with exact advertised name if different)
            print("--- Testing Order Tracker Target ---")
            try:
                order_res = await session.call_tool("order-tracker___get_order", {"order_id": "ORD-001"})
                print(f"Order Response: {order_res.content}")
            except Exception as e:
                print(f"Order Tracker Call Error: {e}")

            # 3. Test Refund Processor
            print("\n--- Testing Refund Processor Target ---")
            try:
                refund_res = await session.call_tool("refund-processor___initiate_refund", {"order_id": "ORD-001", "amount": 29.99})
                print(f"Refund Response: {refund_res.content}")
            except Exception as e:
                print(f"Refund Processor Call Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())