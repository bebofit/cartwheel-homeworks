# Review summary

## Sample

The review set is 100 traces, recorded in `analysis/state/sample_manifest.json`. Six scenarios were set aside after Part A, where they were used to judge the default Langfuse trace view, and they are excluded from every batch: support-0090, support-0118, support-0108, support-0055, support-0232, and support-0036.

| Batch | Traces | How the traces were chosen |
|---|---|---|
| 1, uniform | 15 | Uniform random draw, seed 20261029, from 385 eligible traces |
| 1, cluster | 15 | The trace nearest the center of each of 15 clusters |
| 2, by request type | 30 | Seeded draw within each request type, seed 20261030: refund, order status, and return eligibility (4 each); product search, cancellation, policy question, dispute, out of scope, and account change (3 each) |
| 3, depth | 25 | Targeted retrieval for candidate modes and for close negatives, seed 20261031 |
| 4, final uniform | 15 | Uniform random draw, seed 20261101, from the 159 conversations still unsampled (236 traces) |

Batches 2 and 3 change the mix on purpose. Batch 2 spreads the request types, and batch 3 over-represents the failures under study. The counts below are sample fractions for this review set. Homework 5 estimates prevalence on the full Module 1 trace store.

## Sample fractions

Each of the 100 traces has a present or absent judgment for each of the 7 final modes. Present means the failure occurs on that trace. The judgments are in `analysis/state/labels/<mode>.jsonl` and stored as Langfuse scores named after the mode, with value 1 for present.

| Mode | Present |
|---|---|
| `missing_required_escalation` | 7 of 100 |
| `return_window_without_order_facts` | 7 of 100 |
| `ungrounded_policy_answer` | 6 of 100 |
| `concludes_no_order_after_one_failed_search` | 6 of 100 |
| `unflagged_bad_tool_data` | 5 of 100 |
| `unexplained_decision` | 5 of 100 |
| `unverified_order_match` | 5 of 100 |

40 of the 100 traces have at least one mode present. A trace can carry more than one mode, so the rows above sum to more than 40.

## Stability of the final 15

Of the final 15 traces, 9 contained a failure. Eight of those fit a mode already in the taxonomy. One, support-0195, was a failure not seen earlier. It stays a singleton, since one trace is short of the three positives a mode needs. New modes in the final 15: 0.

## One taxonomy revision

`unverified_order_match` changed after the first search.

The round 3 definition failed a reply that presents an order as the one the user asked about when no tool result shows that order's product or store matching the user's description. Search 1 surfaced support-0018. I rejected it: the reply lists several orders and asks which one the user means, so it presents no order as the user's item. That rejection is the boundary. The saved suggestion records the reason, and the earlier acceptance of the same suggestion is kept in its history.

The definition now fails the reply when it presents the order or acts on it, and a date or total counts as presenting the order. Listing orders for the user to choose from, or asking the user to confirm a match, is not a failure. An order that matches a date the user stated is not a failure either. Search 2 reran the revised rule. It recovered support-0024, which the first rule had missed, and it added support-0059, which I accepted.

Part E applied the revised definition to the whole sample. support-0033 and support-0156 had been accepted under the first definition, and both are labeled absent under the revised one.

A smaller revision came out of the AgentDebug comparison. `order_search_gives_up` was renamed `concludes_no_order_after_one_failed_search`. The definition is unchanged. The old name described the symptom. The positives share one decision: `find_order` returns nothing, and the reply says no order exists without a broader query or `list_my_orders`. The comparison added no mode. support-0112, support-0014, and support-0195 share AgentDebug's "misreads a tool result" category and stay singletons, because each one needs a different product change.

## How the judgments were made

Batch 1 notes are my own. For batches 2, 3, and 4 the coding agent drafted a note per trace, and I accepted or rejected it in the review interface after looking at the trace. All 61 drafts were accepted with no edits. The taxonomy is built from those accepted notes and from my Batch 1 notes.

The searches were retrieval signals, not labels. I reviewed every hit. Of the 10 search suggestions, I accepted 9 and rejected 1, support-0018.

For the structured labels, 18 trace-mode pairs were flagged: a tool-result check disagreed with the draft, or a definition had changed since the note was accepted. I marked all 18 absent in the Labels tab. The other 682 pairs were unflagged drafts, and I had them confirmed in bulk. Three account-change traces, support-0164, support-0165, and support-0167, were first marked present for `missing_required_escalation` and then marked absent. The absent label is the one that stands, and both decisions are in the draft history.

## Relationship to SPEC.md

`SPEC.md` is unchanged.

On support-0164, support-0165, and support-0167 the agent points the user to account settings and does not escalate. I labeled all three absent for `missing_required_escalation`, following `cw-account-security`, which says account changes happen through account settings. ESC-2 says account changes go to a human, so its wording still conflicts with that policy. These three annotations are the reason a SPEC revision is still open. The conflict is recorded as `gap-account-change-route` in `analysis/state/patterns.json`, resolved for labeling only.

`gap-premise-verification` is open. support-0077 notes that the agent would need the order's item and a second lookup before treating a "bought it twice" claim as grounds for a refund. No current mode covers that, and support-0077 is labeled absent for every mode.
