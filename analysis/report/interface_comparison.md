# Interface comparison

The review interface is `analysis/review_app/`. The reference is `analysis/ui/index.html`, served by `analysis/server.py`. Traces came from the live Langfuse project (466 traces). The Module 1 export was not used.

## One design kept from the reference

The reference reads a trace in two columns. The messages sit in the body, and a note sits in the margin beside the span it quotes, with that span highlighted. A tool call and its result share one container, and an agent suggestion is tinted differently from a human note so the two cannot be confused.

I kept all three. In the review app, selecting text in a turn opens a note anchored to that quote, the note renders in the margin, and the quoted words stay highlighted. A tool call, its arguments, and its result render as one block. An agent draft is dashed and labeled as a draft. It becomes an annotation only after I accept it, and a rejection keeps its reason. The reference already treated suggestions as separate from human notes. The review app keeps that split and puts the accept, edit, and reject controls on the draft itself.

## One design changed after inspecting the traces

The reference shows one trace at a time. Cartwheel writes one Langfuse trace per user turn, so a follow-up turn is a separate trace, and the tool results it depends on are on an earlier one. Read alone, the follow-up has no order, no policy page, and no store lookup to check the reply against.

The review app groups traces by `cartwheel.session_id` and shows the conversation in time order. Each turn keeps its own trace id, because notes and the Part E labels attach to a single trace. The sampled turn is marked and scrolled into view on open. Turns from the same conversation that are outside the sample stay visible and are tagged as context, so a later note can point at an earlier tool call without adding that turn to the sample.

## One limitation that remains

A tool result longer than 400 characters starts collapsed, behind a line that only says how long it is. The badge beside the call is a short summary, such as the number of orders returned. A negative price, an empty title, or a delivery dated before the shipment can sit inside that collapsed JSON and never appear in the badge.

That showed up during labeling. Several `unflagged_bad_tool_data` candidates were found by a script reading the tool JSON, on traces I had already marked as having no failure while the result was collapsed. The interface puts the result next to the call, which is the part I can see, and it still hides the contents I would need to judge the failure.
