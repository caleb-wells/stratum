# Stratum

A working answer to one problem: an LLM that drafts geotechnical reports gets confused when
one project spans several jurisdictions. It blends their rules, or applies one city's
requirement to another city's site.

Stratum keeps each jurisdiction's regulations in its own layer, searches each layer
separately, and ties every claim in the answer to the exact source text.

The sample project is fictional. The regulations are real public documents, downloaded
2026-11-24. This is decision support, not engineering advice: confirm requirements against
the currently adopted code.

## The problem with ordinary RAG

Ordinary retrieval embeds every document into one pool and fetches the passages closest to
the question. That fails here for a specific reason: the fault-setback clause in Draper's
standards and the one in Fruit Heights' are nearly the same sentence. They sit next to each
other in vector space, the model receives a mixture, and the answer comes out blended. More
context does not help; it makes the mixing worse.

Three real differences in the loaded documents show what is at stake:

| | Draper City | Fruit Heights City | Ogden Valley |
|---|---|---|---|
| Fault setback method | Lund and others (2020) formula, vs. a table minimum | Batatian and Nelson (1999) formula, vs. Table A-1; never under 15 ft | Flat 50 ft from an active trace |
| "Active" fault (Wasatch) | Displacement in the past 4,000 radiocarbon years | Holocene (about 10,000 years) | No age given in the loaded code |
| Slope stability factor of safety | 1.5 static, 1.0 pseudostatic, 1.3 temporary | 1.5 static, 1.0 pseudostatic, 1.3 temporary | Not specified in the loaded code |

## How Stratum handles it

1. **Jurisdiction is data, not prose.** Every passage is stored with its jurisdiction, document,
   section path, page and source link. The same header is embedded with the text.
2. **A project maps sites to jurisdictions.** A small routing step (Claude Haiku) reads the
   question and decides which jurisdictions it concerns: "Site B" resolves to Fruit Heights;
   "compare the sites" resolves to all three.
3. **One search per jurisdiction, behind a hard filter.** A Draper passage cannot be returned
   as evidence for Fruit Heights, because the Fruit Heights search never sees it. Statewide
   guidance (Utah Geological Survey) is searched separately and labelled as guidance, not law.
4. **Hybrid search, then re-ranking.** Keyword search catches exact things ("1.5", "Section
   8.1.3", "Lund and others (2020)"); vector search catches meaning. The two are merged and
   re-ranked against the actual question.
5. **Section expansion.** When two parts of one section are found, its remaining parts are
   pulled in too, so a formula arrives with its table of minimums.
6. **Tables are parsed as tables.** Plain PDF text extraction scrambled Draper's setback table
   (values landed on the wrong rows). Cells are read individually and written as
   self-describing rows.
7. **Quote-level citations.** Passages go to Claude as separate cited documents. Each claim in
   the answer links to the passage it came from. When a jurisdiction's documents are silent,
   the answer says so instead of borrowing from a neighbour.

```
Browser -> Caddy (HTTPS) -> Chainlit app (login, saved chats, mode picker)
                               | shows what was searched; one chip per cited passage,
                               | which opens the source PDF at the cited page
                               v
                          RAG API (FastAPI, private)
             +-----------------+--------------------+
             v                 v                    v
      Router (Haiku)   Postgres + pgvector    Amazon Bedrock
                       vectors, keywords,     Claude Opus 5.5 / Fable 5.2,
                       metadata filters       Titan embeddings, Cohere Rerank
```

## Try the comparison yourself

The picker at the top left has three setups running the same Claude model:

- **Stratum** - the design above.
- **Baseline · Naive RAG** - one pooled search on the raw question, generic prompt.
- **Baseline · No retrieval** - the model's memory only.

Ask all three: *"What minimum static factor of safety does the jurisdiction for Site C
require for slope stability?"* The loaded Ogden Valley code does not give one. Only an
answer that says so is correct.

## Results

See [`eval/results/results.md`](eval/results/results.md) once the evaluation has been run.
The set is 19 questions written from the verified source text: single-jurisdiction facts,
cross-jurisdiction comparisons, "trap" questions where the right answer is "not specified
here", and one report-drafting task. Each answer is graded for correctness and for
wrong-jurisdiction errors, and retrieval is checked separately.

## What is in the corpus

| Layer | Document | Chunks |
|---|---|---|
| Local | Draper City Geotechnical Engineering Standards (2025-12-16), with Appendices A-E | 145 |
| Local | Fruit Heights City Code Ch. 10.10A (SLEDS) and its appendices | 157 |
| Local | Ogden Valley Code Ch. 108-22, Natural Hazard Areas | 24 |
| State | Utah Geological Survey Circular 128 (2020) | 556 |

Sources and download checks are recorded in [`corpus/sources.yaml`](corpus/sources.yaml) and
[`corpus/provenance.json`](corpus/provenance.json). No paywalled standards are included.

## What I would do next

- **A requirements matrix for the numbers that must never be wrong.** Extract the critical
  parameters per jurisdiction once (factors of safety, setback method, report validity), have
  an engineer confirm them, and look them up exactly at drafting time. Retrieval then covers
  the long tail; the matrix covers the values a report cannot get wrong.
- **Resolve jurisdiction from the map.** Where projects are already placed on a map, a
  point-in-polygon lookup against city and county boundaries would set the jurisdiction
  automatically, including overlapping layers (city, county, state).
- **Track amendments.** Re-download sources on a schedule, diff by section, and flag reports
  drafted against superseded text.
- **Per-firm overlays.** A firm's own standards and past reviewer comments become another
  layer with the same isolation.
- **Evaluation in CI.** The question set here runs in minutes; every prompt, model or
  retrieval change should have to pass it.

## Running it

Local (needs Docker and AWS credentials with Bedrock access):

```bash
cp deploy/.env.example deploy/.env          # then fill in values (the two logins are set here)
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml --env-file deploy/.env exec rag-api python -m app.ingest.cli load
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml --env-file deploy/.env exec rag-api python -m app.ingest.cli verify
open http://localhost:3000
```

AWS (one small ARM instance, about $30/month at full rate):

```bash
cd infra && pnpm install
npx cdk deploy -c domain=<subdomain> -c adminEmail=<you> -c guestEmail=<guest>
# add a DNS A record for the subdomain pointing at the PublicIp output; HTTPS is automatic
```

`cdk destroy` removes everything. `scripts/redeploy.sh` pushes code changes to the running server.

## Safeguards

- Login required. There is no registration: the only accounts are the two set in the environment,
  and repeated failed sign-ins are throttled.
- A daily token ceiling is reserved atomically before each request and settled afterwards.
- The instance role may invoke only the specific Bedrock models used here.
- Containers cannot reach the server's credential endpoint. Short-lived credentials are handed
  to the retrieval service alone, and the setup script refuses to go live if a check of that fails.
- No SSH; access is through AWS Systems Manager. Secrets are generated on the instance and
  stored encrypted in Parameter Store.
- An AWS Budget emails at 50% and 100% of the monthly limit.

## Layout

```
corpus/            source manifest, sample project, provenance
services/rag-api/  routing, retrieval, generation, ingestion, spend guard
services/chat/     Chainlit frontend: sign-in, saved chats, source viewer, theme
deploy/            Docker Compose stack, HTTPS proxy, server script
infra/             AWS CDK (TypeScript)
eval/              questions, runner, results
```

The chat interface is built on [Chainlit](https://github.com/Chainlit/chainlit) (Apache 2.0).
