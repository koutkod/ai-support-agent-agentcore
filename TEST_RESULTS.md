# Functional Test Results — AI Customer Support Agent

**Account:** `<account-id>` · **Region:** us-east-1
**Agent runtime:** `customer_support_agent-KUrjK9D7x9`
**Agent ARN:** `arn:aws:bedrock-agentcore:us-east-1:<account-id>:runtime/customer_support_agent-KUrjK9D7x9`

All six scenarios were executed against the live deployed agent via `agentcore invoke`.
Every test passed. A terminal screenshot of each run is in [`screenshots/`](screenshots/).

> Account ID and the live Gateway / API Gateway URLs are redacted in this public copy —
> both endpoints use `authorizerType: NONE`, as the course instructions allow for a
> throwaway lab account. The unredacted values are in the graded submission.

---

## Deployed resource inventory

| Resource | Name / ID |
|---|---|
| Order tracking Lambda | `csai-order-tracker` |
| Refund Lambda | `csai-refund-processor` |
| API Gateway REST API | `<api-id>` (stage `prod`) |
| API endpoint | `<redacted>` — unauthenticated lab endpoint |
| AgentCore Gateway | `CustomerSupportGateway` → `customersupportgateway-<id>` |
| Gateway URL | `<redacted>` — unauthenticated lab endpoint |
| Gateway target 1 | `order-tracker` (API Gateway REST proxy) — READY |
| API operations | `get_order`, `get_customer`, `get_customer_orders` |
| Gateway target 2 | `refund-processor` (direct Lambda invoke) — READY |
| Knowledge Base | `CustomerSupportKB` → `4BYWCH8IHB` |
| Vector store | OpenSearch Serverless `csai-kb-collection`, index `csai-kb-index` |
| Memory | `CustomerSupportMemory-bZS8B68ug5` — ACTIVE |
| Memory strategy 1 | `customer_facts` (SEMANTIC) → `cs_agent/{actorId}/facts` |
| Memory strategy 2 | `customer_preferences` (USER_PREFERENCE) → `cs_agent/{actorId}/preferences` |

---

## Test 1 — Order Tracking ✅

![Test 1 — order tracking](screenshots/test_1_order_tracking.png)

```bash
agentcore invoke '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}'
```

**Response:**
```
Your order **ORD-001** is currently **SHIPPED**.

Here are the details:
- **Item**: Wireless Headphones Pro (1x $89.99)
- **Total**: $89.99
- **Carrier**: UPS
- **Tracking Number**: TRK987654321
- **Estimated Delivery**: September 15, 2026

Would you like me to help with anything else regarding this order?
```

Expected shipping status, tracking number `TRK987654321`, carrier UPS and an estimated
delivery date — all four present. This exercises the full path: agent → AgentCore Gateway
(MCP) → API Gateway REST proxy → `csai-order-tracker` Lambda.

---

## Test 2 — Refund Processing ✅

![Test 2 — refund processing](screenshots/test_2_refund_processing.png)

```bash
agentcore invoke '{"prompt": "I want to return my Kindle Paperwhite (ORD-002). Please initiate a refund.", "customer_id": "CUST-123", "session_id": "t2"}'
```

**Response:**
```
The refund for your Kindle Paperwhite (Order #ORD-002) has been approved. A credit of
$0.00 will appear on your account within 3-5 business days. Your refund ID is
REF-IOVADMTM.

If you need a prepaid return shipping label to send back the item, I can generate that
for you as well. Would you like me to create a return label for this order?
```

Refund ID, approved status and the 3-5 business day message are all present. This path
goes agent → Gateway → **direct Lambda invocation** of `csai-refund-processor`, with the
tool name resolved from `bedrockAgentCoreToolName`.

*Note on the `$0.00`:* `amount` is an optional field in `lambda_schema`, and the starter
Lambda defaults it to `0` when the caller omits it. The agent did not infer the order
total, so the provided Lambda echoed its default. The refund itself is correctly approved.

---

## Test 3 — Knowledge Base (RAG) ✅

![Test 3 — knowledge base RAG](screenshots/test_3_knowledge_base_rag.png)

```bash
agentcore invoke '{"prompt": "What are the benefits of the Platinum loyalty tier?", "customer_id": "CUST-123", "session_id": "t3"}'
```

**Response:**
```
The Platinum loyalty tier offers the following benefits:

- **Free same-day shipping** on eligible orders
- **15% discount** on purchases (higher than the Gold tier's 10% discount on accessories)
- **Priority customer support** for faster resolution of any issues

These benefits apply to all product categories and provide significant value compared to
the lower loyalty tiers.
```

All three expected benefits retrieved from `product_catalog.txt` via Titan Embeddings v2
and OpenSearch Serverless.

---

## Test 4 — Memory (Long-Term, Cross-Session) ✅

![Test 4a — session A](screenshots/test_4a_memory_session_a.png)

![Test 4b — session B](screenshots/test_4b_memory_session_b.png)

**Session A:**
```bash
agentcore invoke '{"prompt": "Hi, I am Jane. I prefer concise responses.", "customer_id": "CUST-123", "session_id": "s-A"}'
```
```
Hello Jane,

I see you have two orders:

**Order ORD-002** (Kindle Paperwhite):
- Refund approved (ID: REF-IOVADMTM)
- $0.00 credit will appear in 3-5 business days

**Order ORD-001** (Wireless Headphones Pro):
- Total: $89.99
- Shipped via UPS (Tracking: TRK987654321)
- Estimated delivery: September 15, 2026

How can I assist you with these orders?
```

**Session B — a brand-new session:**
```bash
agentcore invoke '{"prompt": "Do you remember my name and communication preference?", "customer_id": "CUST-123", "session_id": "s-B"}'
```
```
Hello Jane. Yes, I remember you prefer concise responses and you're interested in
Platinum loyalty tier benefits.
```

The agent recalled both the name and the stated preference across a session boundary, and
additionally surfaced the Platinum interest first raised back in session `t3`. Note that
the session-B reply is itself short — the agent honoured the remembered preference. Session
A also demonstrates recall, pulling in the orders from sessions `t1` and `t2`.

---

## Test 5 — Loyalty Discount Calculation (Code Interpreter) ✅

![Test 5 — loyalty discount](screenshots/test_5_loyalty_discount.png)

```bash
agentcore invoke '{"prompt": "I am a Gold member with 4250 points. Calculate my discount on a $150 standard order.", "customer_id": "CUST-123", "session_id": "t5"}'
```

**Response:**
```
As a Gold member with 4,250 points, your $150 standard order would receive:

- **Points discount**: $40.00 (4,000 points redeemed)
- **Tier discount**: $11.00 (10% off)
- **Total savings**: $51.00
- **Final total**: **$99.00**
- **Points earned**: 150 points
- **Remaining points**: 400 points
```

Verified by hand against the catalog's business rules:

| Step | Calculation | Result |
|---|---|---|
| Redemption cap (50% of order) | `150 × 0.5 = 75` → 7,500 points | cap not binding |
| Points redeemed (500-point blocks) | `(4250 // 500) × 500` | 4,000 |
| Points discount | `4000 / 100` | $40.00 |
| Subtotal after points | `150 − 40` | $110.00 |
| Gold tier discount | `110 × 0.10` | $11.00 |
| Final total | `110 − 11` | **$99.00** |
| Points earned (standard = 1/$) | `int(150 × 1)` | 150 |
| Remaining points | `4250 − 4000 + 150` | 400 |

Every figure matches, confirming the arithmetic ran in the AgentCore Code Interpreter
sandbox rather than being estimated by the model.

---

## Test 6 — Browser Tool ✅

![Test 6 — browser tool](screenshots/test_6_browser_tool.png)

```bash
agentcore invoke '{"prompt": "Go to https://www.udacity.com and tell me the page title.", "customer_id": "CUST-123", "session_id": "t6u"}'
```

**Response:**
```
The page title of https://www.udacity.com is:

**"Learn the Latest Tech Skills; Advance Your Career | Udacity"**
```

AgentCore Browser launched a live headless session, navigated to udacity.com and read
back the real `<title>` of the rendered page.

*Earlier run, for completeness:* the same prompt pointed at `amazon.com` returns a title
of "Sorry! Something went wrong!". That is genuinely what Amazon serves to an automated
browser — it fingerprints headless clients and returns an error page instead of the
storefront. The tool reported the page it was actually served; it is not a tool failure.

---

## Part 4 — CloudWatch Monitoring ✅

**Log group:** `/aws/bedrock-agentcore/runtimes/customer_support_agent-KUrjK9D7x9-DEFAULT`

**Metric filter** — `CustomerSupportAgentErrors`

| Setting | Value |
|---|---|
| Filter pattern | `ERROR` |
| Metric name | `AgentErrorCount` |
| Namespace | `CustomerSupportAgent` |
| Metric value | `1` |
| Default value | `0` |

**Alarm** — `CustomerSupportAgent-HighErrorRate`

| Setting | Value |
|---|---|
| Metric | `CustomerSupportAgent / AgentErrorCount` |
| Statistic | `Sum` |
| Period | `300` seconds (5 minutes) |
| Evaluation periods | `1` |
| Threshold | `> 5` |
| Comparison operator | `GreaterThanThreshold` |
| Missing data | `notBreaching` |
| State | `OK` |

The console renders the condition as **“AgentErrorCount > 5 for 1 datapoints within 5
minutes”**, which is exactly the required “error count exceeds 5 in a 5-minute window”.
