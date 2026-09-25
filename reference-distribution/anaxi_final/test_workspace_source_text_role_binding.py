"""Role binding at the inference boundary (live finding 2026-09-24, Turn 2/2b): a read document's text used
to reach the model as an escaped string inside the result dict, in the same user-role message as host framing
that addresses the subject as "you", with nothing marking it as the document's own words.  Real model:
reader-addressed documents re-bound to the live interlocutor 17/24 (old) vs 6/24 (this rendering), same
seeds; documents addressed BY NAME to the interlocutor resolved correctly either way (evidence:
completion_evidence/source_text_role_binding_2026-09-24.json).

These tests pin PRESERVATION of provenance, not any interpretation: the stored text is delimited verbatim
under a header saying whose words they are; the header never says whom a document addresses."""
import workspace_delivery as wdl

DOCS = [
    ("notes", {"relative_path": "Garden.md", "authorship": "no author recorded", "content": "You choose the layout.\nJonah cannot choose it for you."}),
    ("library", {"relative_path": "book.txt", "content": "Dear reader, you may skip chapter two.", "next_offset": 38}),
    ("journal", {"entry_id": "entry-1", "author_actor_id": "actor-x", "content": "Priya, you decide the quiet hours."}),
]


def test_every_text_result_carries_its_stored_words_delimited_and_verbatim():
    for scope, result in DOCS:
        text = wdl.render_observation({"scope": f"local_workspace/{scope}", "result": result})
        assert f"<<<\n{result['content']}\n>>>" in text                    # verbatim, unescaped, delimited
        assert "not words of the person you are talking with" in text       # whose words (provenance)
        assert repr(result["content"]) not in text                           # never again an escaped dict value
        meta_line = text.split("\n\n", 1)[0]
        assert "content" not in meta_line and meta_line.startswith("Result: {")


def test_the_header_states_provenance_only_never_an_addressee():
    header = wdl.SOURCE_TEXT_HEADER.format(label="X")
    for forbidden in ("addressed to", "means you", "refers to", "the reader is", "Clark", "Alex"):
        assert forbidden not in header


def test_results_without_text_render_exactly_as_before():
    for result in ({"entries": ["a.md"], "total_count": 1}, {"content": ""}, {"withheld": "exceeds prompt room"}):
        assert wdl.render_observation({"result": result}) == f"Result: {result}"
