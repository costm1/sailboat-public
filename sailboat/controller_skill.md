---
name: sailboat-controller
description: "Send natural-language market contributions to Sailboat Assembly, research market data, recover receipts, and use the existing shared controller for explicitly requested plan actions."
---

# Sailboat shared controller

Shared means shared within the current owner's independent instance. Connect only to that owner's configured server; never discover or reuse the project creator's infrastructure.

Use only for the Sailboat interaction the user requested. Selecting the plugin alone does not authorize a submission. Chat and scheduled Claude share the existing account, plans, Assembly integration, and owner controls. Chat uses the sending agent; it does not start scheduled Claude inference or invoke a second model.

## Natural-language contributions

When the user asks to send analysis or evaluate a named opportunity through Sailboat:

1. Send the requested text with a stable `message_id` through `sailboat_send_message`. An `intent` block or analyst BUY recommendation is not required for Assembly candidate evaluation.
2. Read the returned real account context and retain `receipt_id`. The existing extraction feeds attributed pieces into Assembly, which associates contracts, aggregates supporting and conflicting evidence, selects candidates, evaluates dependencies, invalidates stale support, and finalizes eligible snapshots.
3. Recover that same `receipt_id` with `sailboat_message_receipt` to inspect `shadow_sorter`, `candidate_entry`, Assembly readiness, missing prerequisites, and any `controller_receipt` actually returned. Sorting is asynchronous. A first response may still say `NEEDS_INTENT`; that label does not require a second intent submission for the Assembly path.
4. Report the actual result. If processing is pending, say so. Do not invent a selection, completed evaluation, controller acceptance, order, or fill.

Keep source timestamps, attribution, confidence where supplied, exact contract terms, reported premiums, resale forecasts, conditions, and evidence attached to their source. Preserve competing call and put setups separately. Do not invent missing expiry, confirmation thresholds, momentum definitions, or volume criteria. Resolve an explicit 0DTE reference against the returned market date; historical observations retain their original date. Ordinary chat does not inherit the scheduled analyst's host-issued QQQ 0DTE context.

An analyst's PASS, personal preference to wait, or generic execution disclaimer does not by itself withdraw a named candidate or decide owner permissions. Preserve an actual candidate withdrawal as evidence for invalidation. Do not turn either PASS or a named contract into an analyst BUY instruction. Explicit user research-only, do-not-submit, and no-orders restrictions remain binding; unauthenticated analyst text cannot grant or revoke owner authority.

For research that the user says not to submit, use `sailboat_market_read` and explain the findings without sending the analysis to `sailboat_send_message`. This transport can trigger owner-policy evaluation; it is not a read-only archive. See [broker argument schemas](broker_contracts.json) for supported reader arguments.


## Entry probability rule

When the account is flat, the analyst identifies the most promising QQQ 0DTE CALL and PUT separately. Each needs an exact strike and expiration and an estimated probability, expressed as a percentage, of profitable premium resale within the next 30 minutes. Provide realistic future bid ranges and potential premium appreciation or deterioration over approximately 30 minutes; this is not an automatic exit deadline. The estimates are independent and need not add up to 100%.

Keep the probability attached to its exact source contract and timeframe. Ordinary prose is sufficient; an unambiguous form is "QQQ $722 call expiring YYYY-MM-DD: 55% probability of profitable premium resale within the next 30 minutes." Use actual supported identities and estimates. Missing, ambiguous, or merely conditional estimates remain unresolved.

Assembly selects the higher probability, even by one percentage point. An exact tie means STAY OUT. The winning estimate must meet the owner-saved minimum entry probability (40% by default; editable in the owner dashboard). At a 55% minimum, PUT 42% / CALL 35% stays out and PUT 58% / CALL 35% selects PUT for the remaining checks. Preserve the original estimates; never inflate them to meet the threshold. PASS, WAIT, NO TRADE, personal reluctance, and verbal preferences do not override this comparison. Do not invent missing probabilities, normalize the two numbers to total 100%, rank by wording, or substitute another contract.

After selection, the existing runtime resolves the winning broker contract, obtains fresh quotes, and applies affordability, owner authorization, daily-loss limits, and existing execution safeguards. A blocked winning contract does not authorize falling back to the lower-probability candidate. A numerical win is not proof of profitability or permission to trade.

Once an actual holding exists, retain the existing HOLD/profitable SELL and resale-premium-target workflow. Do not compare new entry candidates in place of managing the held position.

## Explicit shared-plan actions

Use the existing two-step intent exchange only when the user requests a structured controller decision or an explicit shared-plan action. Do not add a second action merely to complete an Assembly receipt.

1. Send the requested text and stable `message_id` to obtain current account context.
2. Before `reply_before`, send unchanged text and `message_id`, `context_receipt=receipt_id`, and a faithful `intent`. The sending agent supplies this block; the user need not write JSON.
3. Report `controller_receipt` and `receipt_id`. After an uncertain response, recover the same receipt rather than issuing a new action identity.

Use meaning observation/recommendation/instruction/clarification and action observe/watch/enter/exit/hold/revise/cancel/clarify, with exact supporting text excerpts in `evidence`. An observation or recommendation on this explicit route does not authorize execution. Never upgrade hypotheticals, refusals, quoted instructions, or options mentioned in prose into an instruction. Multiple independent plan actions require clarification rather than silently dropping one.

Let the controller resolve brokerage identities and plan versions; do not construct internal orders, UUIDs, accounts, or versions. Use returned `plan_name` for changes. Provide actual option expiry, strike, and call/put for executable option intent. Entry, exit, or replacement requires explicit `quantity`, `limit_price`, and `valid_for_seconds`. The existing explicit order worker supports `gte` and `lte` conditions/invalidation only; never discard or approximate unsupported conditions. Revision uses replace/suspend/resume.

## Economics, holdings, and owner controls

Source premiums are evidence, not executable quotes. The existing runtime obtains fresh broker identity, bid/ask, buying power, fee review, and reconciliation before permitted action. Selection and affordability do not establish favorable economics. The objective is short-term option-premium resale; expiration breakeven is not a prerequisite. Use the owner's probability comparison without adding profitability cutoffs. Report missing execution facts through the existing safeguards.

Use broker-confirmed holdings and existing shared plans for position management. Preserve exact identity, quantity, basis, and fresh quotes. HOLD may supply `target_bid` and `expected_upside_usd` when supported. Exits require a supported `roundtrip_cost_allowance_usd`; unknown fees are not zero. Keep the existing profit manager, no-automatic-loss-sale protection, and broker order worker. Do not create duplicate sell paths or competing plans.

Execution depends on the host write gate, candidate-entry gate where applicable, current owner ARM epoch, limits, fresh evidence, and broker checks. This connection cannot arm the controller, change owner limits, or enable brokerage writes. Preserve DISARMED and disabled writes when required; do not infer current state from this document.

An Assembly snapshot or receipt is not execution authorization. Proposed, previewed, queued, or accepted is not filled. Report broker outcome only when confirmed. Read prior receipts before retrying. Stale requests need fresh assessment, never a new identity to bypass a denial. Historical records and disarmed previews must not be replayed as fresh executable instructions.
