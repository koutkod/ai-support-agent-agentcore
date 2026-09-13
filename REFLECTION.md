# Reflection

**Design decision.** The trickiest design call was the loyalty discount formula in
`calculate_loyalty_discount`. The brief only gives loose rules ("100 points = $1",
"minimum redemption 500 points", tier percentages), not an exact order of operations, so
I made two decisions explicit in code rather than leaving them implicit: points
redemption is capped at 50% of the order total *before* the tier discount is applied, and
the tier discount then applies to the post-points subtotal rather than the original
total. I chose that order because it matches how the product catalog describes tier
benefits ("15% discount" layered on top of whatever else applies) — applying it first
would silently change the effective value of the points-redemption cap. I kept the whole
calculation as a single self-contained code string with no external references, since the
Code Interpreter sandbox has no access to the rest of the module.

**Challenge.** The hardest part was wiring the API Gateway target into the AgentCore
Gateway, which failed twice for reasons the error messages only half-explained. First,
`CreateGatewayTarget` rejected the name `order_tracker` against the pattern
`([0-9a-zA-Z][-]?){1,100}` — underscores aren't allowed, despite the instructions naming
the target that way, so it became `order-tracker`. Then the target reached FAILED with
"Failed to parse OpenAPI specification: attribute paths.'/orders/{order_id}'(get).responses
is missing". The Gateway builds its tool list from the REST API's *exported* OpenAPI
document, and an API Gateway method with only an integration — no declared method
response — exports without a `responses` block and is therefore unparseable. The fix was
`put_method_response` for `200` on all three methods plus `operationName` on each (tool
names derive from `operationId`), then redeploying the stage. I verified the fix by
exporting the spec with `get_export(exportType="oas30")` and asserting both
`operationId` and `responses` were present before recreating the target, rather than
recreating it hopefully and waiting to see.

**Production consideration.** The Gateway uses the `NONE` authorizer, which the
instructions flag as acceptable only for a temporary lab account. In production I'd put
real auth in front of every target (IAM SigV4 or an OAuth/Cognito authorizer) so the
order and refund tools aren't callable by anyone who obtains the Gateway URL, and I'd add
per-customer authorization inside the Lambda handlers — right now `order_tracker.py`
returns any customer's order to whoever asks for that order ID, with no check that the
requesting `customer_id` actually owns it.
