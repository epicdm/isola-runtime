---
name: query_agent
description: Ask a subordinate agent a question or delegate a task to them on the owner's behalf, then relay the result back to the owner.
---

# Query Agent

**When to use:** Owner directs Chief to ask another agent something, or to delegate work to them. Triggers: "ask Rex about...", "check with Joey on...", "what does Cash say about...", "get Rex's take on [customer name]", "have Joey pull the lead pipeline", "find out from Rex why that customer was upset".

## How to use send_message_to_agent

Call `send_message_to_agent` with three required fields:

- `agent_name` — target agent's name exactly as listed in your relationships.md (e.g. "Rex", "Joey", "Cash")
- `message` — a precise question or task instruction, stripped of owner's framing
- `msg_type` — one of three values:

| msg_type | Use when | Example |
|---|---|---|
| `consult` | Quick factual question needing an immediate answer | "What was the last escalation reason code?" |
| `task_delegate` | Target needs to do work and return results | "Summarize today's customer conversations by topic." |
| `notify` | One-way FYI — no reply expected | "Owner approved your pending outreach to lead #42." |

When in doubt between consult and task_delegate, prefer `task_delegate`. It guarantees a response; consult may time out if the agent is busy.

## Procedure

1. **Identify the target agent** from the owner's message. If ambiguous, ask: "Should I check with Rex or Joey on this?"
2. **Construct a precise query.** Strip the owner's framing; extract the factual question or task.
3. **Choose msg_type.** Factual answer needed now → `consult`. Analysis or summary needed → `task_delegate`. Just informing → `notify`.
4. **Invoke send_message_to_agent.** Await the response.
5. **Relay the result** to the owner with attribution.

## Example

Owner asks Chief: *"Ask Rex about the last escalation."*

Chief:
- Identifies target: Rex
- Constructs precise query (strips framing): "What was the last escalation trigger and reason code in the past 24 hours?"
- Invokes:
  ```
  send_message_to_agent(
    agent_name: "Rex",
    message: "What was the last escalation trigger and reason code in the past 24 hours?",
    msg_type: "consult"
  )
  ```
- Awaits response.
- Relays to owner: "Rex reports: [summary of response]. Want me to act on this?"

## Scope

- Read-only by default — use this to gather information, not to trigger writes, sends, or bookings through another agent. Consequential actions require explicit owner approval; escalate_to_eric if needed.
- If the target agent doesn't respond (task_delegate times out), surface: "{agent} did not respond — flagging for owner."

## What NOT to do

- Don't editorialize beyond summarizing. Relay the agent's response accurately.
- Don't re-query the same agent in a loop for the same question. One query per owner instruction.
- Don't issue write/send/book instructions through this tool — surface the request to Eric via escalate_to_eric instead.
