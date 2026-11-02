---
name: sailboat
description: "Send user-requested text and interpret it into a small meaning block for Sailboat simulated plan handling; recover the actual controller receipt. No second model or live orders."
---

# Sailboat conversation interface

Use when the user asks to send Sailboat a message, assessment or plan instruction.
Selecting the plugin alone is not authorization to transmit unrelated content.
You are the sending agent and interpreter. Do not call another model. The user speaks
naturally; you supply a small intent block while Sailboat resolves its machinery.

1. Send the requested `text` and a stable `message_id` through `sailboat_send_message`.
2. Read the returned `context`. It contains simulated plans, positions and approved
   contract descriptions, without account numbers, ARM state or internal plan versions.
3. Call the same tool with unchanged text/message_id, `context_receipt` set to the
   returned receipt_id, and `intent`. Do this within the returned reply_before deadline.
   Do not stop after NEEDS_INTENT or make the user construct this block.
4. Report the actual `controller_receipt.result` and receipt ID. If uncertain, retry
   identical arguments or read `sailboat_message_receipt`. Never claim an order or fill
   from RECEIVED, NEEDS_INTENT, PROPOSED, OBSERVED_DISARMED or PENDING.

Intent uses `meaning` (observation, recommendation, instruction, clarification),
`action` (observe, watch, enter, exit, hold, revise, cancel, clarify), exact excerpts from
text in `evidence`, and only applicable terms: symbol, human plan_name, instrument,
quantity, limit_price, condition, invalidation, direction and reasoning. A revision needs
revision=replace/suspend/resume. An option instrument is kind=option, expiry=YYYY-MM-DD,
strike and right=call/put. A stock uses kind=stock. Resolve today/0DTE to context.market_date.
Conditions use symbol, op (gt/gte/lt/lte/cross_above/cross_below) and price.

Do not construct brokerage orders, contract UUIDs, account numbers, internal plan IDs or
versions. Use a returned human plan_name for changes to existing plans. If several plans
or positions fit, ask to clarify rather than choosing arbitrarily. Split multiple
independent actions; never silently discard a condition or part of an instruction.
An explicit entry/exit needs specified quantity and limit premium. Do not invent missing
terms, size from a budget, treat an option mention as buy, or treat a bearish view as sell.
Observations do not mutate plans. Recommendations may create non-executing proposals;
only explicit instructions can request execution, and owner controls still decide.
Preserve refusals, hypotheticals, research-only, do-not-submit and no-orders restrictions.
Do not route a refused transaction through prose. Intent must faithfully describe text,
not override it. Do not report live profitability from a fee-free simulation.

This connection handles simulated plans only and does not invoke live brokerage orders,
arm, change limits, or use Claude subscription capacity. If a receipt is rejected for
stale context or changed state, report the reason; do not automatically change IDs or
reissue an old trade. NEEDS_INFORMATION requires clarification. Existing legacy messages
are preserved, never automatically replayed through the new pathway.
