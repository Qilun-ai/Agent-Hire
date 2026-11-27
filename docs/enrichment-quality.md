# Job requirement extraction

Posting enrichment uses the job description and title, without personal profile
or résumé data. The optional generative extractor remains disabled by default;
Jev supplies bounded classification opinions, not open-ended entity extraction.
No new provider, fallback or classifier adapter is enabled by these fixes.

The section parser now recognizes “The qualifications you need,” “What you
bring,” required/preferred technical and professional expertise, education
headings, and common required/preferred skills headings. “A significant
advantage,” “an advantage,” “optional” and “not required” identify preferred
qualifications within a required section. Role and company-benefit headings
bound eligibility sections so later company/benefit text remains a mention.

Offsets and description hashes refer to the unchanged normalized input. IBM's
newly retrieved detail text retains explicit heading separators. Existing feed
rendering, dictionary/policy files and frozen acquisition fixtures stay intact.

An explicitly truncated source body yields `incomplete_input`, retaining any
positive evidence while preventing an absent skill from becoming a negative
filter result. A shorter capped observation does not taint a selected full body.
The worker includes the parser revision and input status in its cache identity,
preserving the `job-document-v1` storage format while preventing reuse of old
parser results during the next acquisition or explicit reevaluation.

The web panel collapses repeated canonical labels and identical OR groups for
display. Different OR choices and standalone requirements remain separate;
stored evidence, item IDs and filter semantics are unchanged. Disabled optional
extraction and incomplete source text receive explanations in the panel.

Existing immutable search snapshots are not rewritten. Full IBM descriptions
require a new explicit scrape, and Python parser changes require a corpus service
reload before that acquisition. No live scrape, quota-consuming probe, provider
call or stored-job backfill is part of offline verification.
