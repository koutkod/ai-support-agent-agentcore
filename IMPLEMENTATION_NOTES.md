# Implementation notes

Internal notes on how each TODO was implemented and why, for anyone
extending this agent later. Not part of the rubric checklist itself.

## Config (TODO 1-3)

- `model_id = "global.amazon.nova-2-lite-v1:0"` — a cross-region inference
  profile, kept as given in the starter file. Make sure **Amazon Nova Lite**
  is enabled under Bedrock → Model access before the first invoke, or every
  tool call fails at the model layer, not the tool layer, which is confusing
  to debug.
- `_bedrock_runtime` is a `bedrock-agent-runtime` client (note: *not*
  `bedrock-agentcore`) — that's the older Knowledge Bases Retrieve API
  client, a separate service from AgentCore proper.

## Namespaces + Memory Hook (TODO 4-5)

- `MemoryClient.get_memory_strategies()` normalizes the strategy id/type
  field names but **not** the namespace field name — the raw response can
  carry either `namespaceTemplates` (current) or `namespaces` (legacy),
  depending on how/when the Memory resource's strategies were created.
  `get_namespaces()` checks both.
- A memory record from `retrieve_memories()` has its text under
  `record["content"]["text"]` (a nested structure, not a flat string).
- `retrieve_memories()` requires exactly one of `namespace` or
  `namespace_path`; this project always passes a fully-resolved `namespace`
  (the template with `{actorId}` substituted), never `namespace_path`.
- Distinguishing "a plain customer message" from "a tool-result message" is
  done by checking `content[0]` for a `"text"` key rather than a
  `"toolResult"` key — both get added to `agent.messages` with
  `role: "user"`, so checking the role alone isn't enough.
- `MemoryHook` is constructed fresh per invocation (inside `invoke()`), not
  once at module load, because it needs the per-request `actor_id` and
  `session_id`.

## Knowledge Base tool (TODO 6)

- Guard clause checks `KB_ID.startswith("<")` rather than just falsiness, so
  it also catches the unedited placeholder value, not only an empty string.

## Loyalty discount tool (TODO 7)

- See `REFLECTION.md` for the redemption-cap/tier-discount ordering
  rationale.
- `code_session(REGION)` is a context manager; `client.invoke("executeCode",
  {...})` returns `{"stream": <event iterator>, "sessionId": ...}`. The
  event you want is `event["result"]["content"][i]["text"]` — that's the
  sandboxed code's own stdout (the `print(json.dumps(result))` line),
  which is why the code string ends with a `print`, not a `return`.
- The fallback path (except block) intentionally sets `points_redeemed: 0`
  rather than attempting the points math without the sandbox — the points
  cap/floor logic is exactly the kind of arithmetic this tool exists to get
  right, so it's not worth reimplementing loosely in the fallback.

## Agent entrypoint (TODO 8)

- The `with gateway_client:` block wraps the *entire* agent invocation, not
  just `list_tools_sync()`. Closing the Gateway connection after listing
  tools but before the agent actually calls one of them will surface as the
  agent's tool call silently failing partway through a conversation.
- `agent.invoke_async()` (not `agent()`) since `invoke()` is itself `async
  def` — using the sync call here would block the event loop.
- If you see event-loop conflicts once the Browser tool (Playwright) and the
  Gateway's MCP background thread are both active in the same request,
  `nest_asyncio` is already a pinned dependency in `pyproject.toml` for
  exactly this — add `import nest_asyncio; nest_asyncio.apply()` near the
  top of `main.py` if it comes up.

## Known gaps worth knowing about, not required by the rubric

- `order_tracker.py` (Lambda, provided as-is) doesn't check that the
  `customer_id` on the request actually owns the `order_id`/`customer_id`
  being looked up — anyone who can reach the Gateway can look up any order
  or customer record. Fine for this lab; not fine to ship as-is.
- The Gateway uses the `NONE` authorizer per the lab instructions. Delete it
  after finishing the project, as the instructions themselves say.
