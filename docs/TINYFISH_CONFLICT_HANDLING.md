# TinyFish source conflict handling

The batch pipeline now compares evidence from official source pages before it
spends an LLM call. The comparison is deterministic Python; TinyFish only
discovers and fetches sources.

## Decision rules

- Benefit amounts are compared only when both claims have a recognized shared
  unit, such as annual, monthly, per instalment, per family, or per member.
- Currency amounts and percentages are separate measures.
- Several amounts listed by one source are treated as possible benefit tiers,
  not as a contradiction. Their scope stays unresolved unless the source text
  makes the categories clear.
- Government savings and scheme budget/outlay values are not beneficiary
  benefits.
- Two explicit, differently worded complete document lists are marked
  conflicting. A single Aadhaar or land-record rule remains partial evidence.
- A narrowly recognized PDF footnote artifact can be repaired only when two
  other official source URLs independently support the clean value. The
  malformed text remains in the audit warnings.

If the checker finds a direct conflict or cannot classify multiple values,
that field is withheld and the pipeline searches official pages for that field.
If it remains unresolved, the seed ends as `evidence_incomplete` before LLM
parsing; it does not enter MongoDB or Qdrant. A later scheduled run can retry
it if the periodic job is configured to rerun the seed. This implementation
does not create a human-review task or configure that scheduler.

## Full-field evidence search

Run `python -m tools.batch_ingest --coverage-only --full-field-search` to do a
coverage pass over all six fields: identity, eligibility, benefits, documents,
application route, and continuation. Searches are serial per seed to avoid
overlapping model/browser work. The report keeps exact matching passages with
source URL, content hash, fetch timestamp, publication date when available,
and the query that retrieved the source. Documents are labelled as a complete
checklist passage, partial rule, or unknown. These are evidence leads, not
verified facts or parsed schema values. The command requires coverage-only
mode, so it cannot call the LLM or write MongoDB/Qdrant records. A
`ready_for_parse` coverage result means there is enough source material to
attempt schema parsing; it does not certify that every field was found or
that the scheme is currently active.

The regular ingestion path also classifies document evidence before parsing.
A single Aadhaar/data rule or generic "documents" mention is retained as
partial evidence; it cannot satisfy the checklist gate. Only an explicit
checklist passage can clear that gate for citizen indexing. If the packet has
only partial or unknown document evidence, a parsed candidate stays out of
Qdrant.

The searches can take several minutes per scheme because TinyFish calls are
serial. An unreachable official site or a JavaScript shell is recorded as a
failed/empty source; it is not treated as proof the scheme lacks that detail.

## Limits

This is a conservative screen, not a semantic fact checker. It compares only
visible phrases and units. It cannot reliably infer hidden applicant groups,
policy versions, legal precedence, or whether two differently worded claims
mean the same thing. Ambiguous wording must remain unknown; the checker must
not choose a winner just because a source is newer or appears more official.

## Checks performed

Coverage-only TinyFish runs were made for PM-Kisan, PM Fasal Bima Yojana, and
PM Surya Ghar. They made no LLM calls and no database writes. The conflict
review classified PM-Kisan's annual amount as corroborated. PMFBY's benefit
amount remained unresolved at coverage review. PM Surya Ghar's packet contained
different subsidy values without enough shared context to compare them, so the
benefit stayed unresolved rather than being called a direct contradiction. An
initially misread government electricity-savings amount was excluded from
citizen benefit claims. Unit tests also exercise a direct annual-amount
conflict and two disagreeing explicit document lists. A full-field coverage
run captured six serial searches each for PM-Kisan and PMFBY. PM-Kisan's
packet had identity, eligibility, benefit, application, and continuation
passages, but only partial document evidence; the complete-checklist query
found no parseable source. PMFBY's search left benefits unresolved and found
only partial document rules. The evidence packet includes exact quotes and
source hashes, but those passages still require schema parsing and
field-level validation before they can be stored as citizen-facing records.
