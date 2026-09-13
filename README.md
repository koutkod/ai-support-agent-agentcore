# AI Customer Support Agent — Amazon Bedrock AgentCore

A production-shaped customer support agent for a fictional e-commerce store, built on
**Amazon Bedrock AgentCore** and the **Strands** SDK. Submitted for the Udacity
*Future AWS Agent Engineer* nanodegree (course `cd14763`).

The agent handles a support conversation end to end: it looks up orders, issues refunds,
answers policy questions from a knowledge base, remembers the customer between sessions,
does exact discount arithmetic in a sandbox, and can browse the live web.

## Capabilities

| Capability | How it works |
|---|---|
| Order tracking | Lambda behind an API Gateway REST API, exposed as MCP tools through an AgentCore Gateway target |
| Refund processing | Lambda invoked **directly** by a second Gateway target; the tool name arrives in `context.client_context.custom["bedrockAgentCoreToolName"]` |
| Product & policy Q&A | Bedrock Knowledge Base (Titan Embeddings v2 + OpenSearch Serverless) over `product_catalog.txt` |
| Cross-session memory | AgentCore Memory with a semantic strategy (`customer_facts`) and a user-preference strategy (`customer_preferences`) |
| Loyalty discounts | AgentCore Code Interpreter runs the pricing arithmetic in a sandbox rather than letting the model estimate it |
| Live web browsing | AgentCore Browser |

## Architecture

```
                    ┌──────────────────────────────┐
  agentcore invoke  │   AgentCore Runtime          │
  ────────────────▶ │   main.py  (Strands Agent)   │
                    └──────┬───────────────────────┘
                           │
         ┌─────────────────┼──────────────────┬─────────────────┐
         │                 │                  │                 │
         ▼                 ▼                  ▼                 ▼
  AgentCore Gateway   Knowledge Base    AgentCore Memory   Code Interpreter
       (MCP)          (Titan v2 +        (semantic +        + Browser
         │             OpenSearch)        preferences)
         │
    ┌────┴────────────────────┐
    ▼                         ▼
 order-tracker           refund-processor
 (API Gateway REST)      (direct Lambda)
    │                         │
    ▼                         ▼
 order_tracker.py       refund_processor.py
```

## Repository layout

```
main.py                    # the agent — all 8 sections implemented
pyproject.toml             # dependencies (uv)
product_catalog.txt        # knowledge base source document
lambda/
  order_tracker.py         # GET /orders/{id}, /customers/{id}, /customers/{id}/orders
  refund_processor.py      # initiate_refund, check_refund_status, get_return_label
  lambda_schema            # MCP tool schema for the refund Gateway target
TEST_RESULTS.md            # verbatim output for all 6 required test scenarios
screenshots/               # terminal screenshots of each test run
REFLECTION.md              # design decision, challenge, production considerations
IMPLEMENTATION_NOTES.md    # notes on how each section was implemented
```

## Running it

```bash
uv sync

# local
uv run main.py '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "s1"}'

# deploy
uv run agentcore configure --entrypoint main.py --name customer_support_agent \
    --region us-east-1 --disable-memory --non-interactive
uv run agentcore deploy

# invoke the deployed agent
uv run agentcore invoke '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}'
```

The four constants at the top of `main.py` (`GATEWAY_URL`, `KB_ID`, `REGION`, `MEMORY_ID`)
point at the AWS resources created in Part 1 and must match your own account.

## Test results

All six required scenarios pass against the deployed agent. Full transcripts are in
[`TEST_RESULTS.md`](TEST_RESULTS.md), and a terminal screenshot of every run is in
[`screenshots/`](screenshots/).

| # | Scenario | Result |
|---|---|---|
| 1 | Order tracking | Returns `SHIPPED`, tracking `TRK987654321`, carrier UPS |
| 2 | Refund processing | Refund ID issued, `APPROVED`, 3–5 business days |
| 3 | Knowledge base (RAG) | All three Platinum tier benefits retrieved |
| 4 | Cross-session memory | Recalls name and stated preference in a brand-new session |
| 5 | Loyalty discount | $40 points + $11 tier → **$99.00**, 400 points remaining |
| 6 | Browser tool | Live page title and body text extracted |

## Two things worth knowing

**Gateway target names cannot contain underscores.** `CreateGatewayTarget` validates
against `([0-9a-zA-Z][-]?){1,100}`, so the targets are `order-tracker` and
`refund-processor` rather than the underscored names used in the course instructions. The
console enforces the same rule, and `refund_processor.py` strips the target prefix either
way.

**An API Gateway target needs declared method responses.** The Gateway builds its tool
list from the REST API's *exported* OpenAPI document. A method with only an integration
and no method response exports without a `responses` block, and the target fails with
`Failed to parse OpenAPI specification: ... .responses is missing`. Declaring a `200`
method response on each method — and setting `operationName`, since tool names derive from
`operationId` — fixes it.

## Security note

The AgentCore Gateway in this project uses `authorizerType: NONE` and the API Gateway
methods use `authorizationType: NONE`, which the course instructions accept for a
short-lived lab account. Anyone holding those URLs can invoke the order and refund tools.
Do not reuse this configuration outside a throwaway account, and tear the stack down once
the project has been assessed.
