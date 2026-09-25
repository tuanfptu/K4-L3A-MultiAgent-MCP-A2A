# L3A Architecture Record

## 1. System overview

`inputs/<case_id>.json` → coordinator → order, payment, shipment and policy specialists
→ deterministic issue classifier → verifier → `outputs/<case_id>.json`.
Every MCP response consumed by a specialist emits a `tool_result_consumed` event.
The CLI emits `case_received` and `case_finalized`; the workflow emits
assignment, handoff, policy decision and verification events.

The customer message and claim topics identify questions to assess. They do not
establish facts. The classifier uses MCP data within the case's observation
window, from the order purchase time through `opened_at`.

## 2. Agent ownership

| Actor | MCP tools | Output / handoff |
| --- | --- | --- |
| Coordinator | Discovery only | Assigns specialists; collects evidence and failures |
| Order agent | `get_order`, `get_order_items`, `get_sellers` | Order status, item and seller IDs |
| Payment agent | `get_payment_timeline`, `get_refund_timeline` | Captures, reconciliation and refund state |
| Shipment agent | `get_shipment_summary` | Delivery timing and seller handoff |
| Policy agent | `get_policy` | Case status, action and amount for an evidenced issue |
| Verifier | None | Schema compatible result, provenance linkage and failure flag |

Tool calls and cases run sequentially through a single authenticated MCP
session. A timed out read-only call is retried once. Tool discovery is cached
for that session. No specialist requests unrelated customer history or product
context. Refund and seller details are requested for cases whose claim topic
requires those specialist investigations; the topic never determines the result.

## 3. A2A protocol

The observable envelope is the trace event schema: `case_id`, actor, target,
decision code, tool name and evidence refs. `task_assigned` starts each
specialist's bounded task. After one MCP call the specialist emits
`tool_result_consumed` and `handoff` with `EVIDENCE_READY`, or a handoff
with `EVIDENCE_UNAVAILABLE`. A specialist cannot delegate onward, so there is
no handoff cycle. MCP client timeouts bound external waits.

## 4. Evidence lifecycle

The gateway validates every response against the public evidence schema.
Only refs returned by a successful MCP call for the current `case_id` are
eligible for the output. The classifier ignores events before purchase and
after `opened_at`. Rows with a dated shipping limit outside that window are
excluded from item totals. The output cites evidence that supports the
selected issue plus policy; claim refs are subsets of those output refs.

Issue precedence is order cancellation/unavailability, refund failure/pending,
payment mismatch, duplicate captures, late delivery, valid split payment,
then unsupported claim. Refund and payment event times take priority over
undated summary rows. Policy supplies the remedy only after classification.
Seller IDs come from the scoped item evidence rather than policy example IDs.

## 5. Failure policy

| Failure | Retry | Fallback | Trace |
| --- | --- | --- | --- |
| MCP timeout / error | One bounded retry for timeout; no retry for tool error | Record unavailable tool; if required, classify as insufficient evidence | `EVIDENCE_UNAVAILABLE` |
| Missing tool | No | Same as above | Verification marks partial evidence |
| Conflicting time scopes | No | Exclude out-of-window records | Consumed refs retained only if relevant |
| Invalid MCP response | No | Treat as unavailable | `EVIDENCE_UNAVAILABLE` |
| Missing policy rule | No | Investigation, zero proposed refund | `policy_decided` |

The fallback never manufactures evidence or money values. An unavailable
optional refund tool does not prevent classification of an order or delivery
issue. An unavailable required tool lowers confidence and yields
`insufficient_evidence`.

## 6. Verification invariants

- Output `case_id` matches input and passes the public JSON Schema.
- Entity IDs are taken from scoped MCP data, with duplicates removed.
- Claimed full refund is supported only when the policy amount covers the
  captured amount as of `opened_at`.
- One refund line sums to the recommended amount when the amount is positive;
  zero means no refund line.
- Policy action, status and responsible party match the classified issue.
- Every output evidence ref was returned and consumed in this case's trace.
- Confidence is within `[0, 1]` and is reduced on missing evidence.

## 7. Reproducibility

The workflow is deterministic Python 3.11+ code without an LLM, randomness
or hidden prompts. Dependencies are bounded in `pyproject.toml`. Run
`day09 validate-inputs`, `day09 run`, `day09 validate`, and
`day09 package --output dist/submission.zip`. The authenticated MCP session
and competition audit bind evidence refs to the current team and run. Never
reuse a previous run's output or trace.

## 8. Visual demo

`day09 demo` starts a loopback-only HTTP server. The home page shows the
coordinator, specialist agents, MCP Gateway, verifier, and output flow; the
case demo sits below the operation map. Clicking an agent reveals its tool
ownership. Replay reads existing
case output and trace without another MCP call. Live mode invokes
`solve_case` for one selected case and records the tool responses for display.
Its trace is written under `traces/demo/`; it does not alter the competition
submission artifacts. The team key stays server-side. Live mode reports a
Gateway error if no evidence was returned, rather than presenting an
unsupported decision as a completed investigation.
The `/agents` route is a separate explainer page for the six workflow roles,
their owned MCP tools and the operation map. It links to a preselected replay
case on the demo home page. The roles are functions within one deterministic
Python workflow, not separate LLM services.
