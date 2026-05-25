# sn-mdm Architecture

`sn-mdm` is a permanent, highly compact content-pack builder for
on-device agents. It crawls freely accessible podcast transcripts
and official resource hubs, normalises them, chunks them under a
strict rights gate, and bundles everything into a single
zstd-compressed SQLCipher database (the `.pack` artefact).

The design borrows directly from two upstream substrates so any
engineer familiar with either repo can pattern-match into this
one without an onboarding period.

## Substrate cross-walk

### Borrowed from `kennguy3n/knowledge`

| `knowledge` concept                              | `sn-mdm` realisation                                                              |
| ------------------------------------------------ | --------------------------------------------------------------------------------- |
| `crates/evidence_store` append-only `evidence`   | `pack_core::schema::SCHEMA_SQL` `episode` + `chunk` tables (triggers reject UPDATE / DELETE) |
| BLAKE3 content-hash dedup column                 | `chunk.content_hash BLOB` + `idx_chunk_hash` + `governance_log.content_hash`      |
| FTS5 virtual table with `unicode61 remove_diacritics 2` | `chunk_fts` virtual table                                                  |
| Embeddings cache v3 — composite `(id, model_tag)` PK | `chunk_embeddings (chunk_id, model_tag)` PK                                   |
| `connector_framework::Connector` trait surface   | `crawl.crawlers.base.BaseCrawler` lifecycle (`initial_sync`, `incremental_sync`, `fetch_transcript`, `normalize`, `chunk`) |
| `SyncRunResult` + cursor pagination              | `crawl.crawlers.base.SyncState` (cursor + `seen_content_hashes`)                  |
| Tombstone / cryptographic-forgetting pattern     | `governance_log.deprecated` overlay (audit trail is append-only; status flows through a new row) |

### Borrowed from `kennguy3n/chat-storage-search`

| `chat-storage-search` concept                    | `sn-mdm` realisation                                                              |
| ------------------------------------------------ | --------------------------------------------------------------------------------- |
| `QueryEngine` multi-lane merge                   | `pack_core::search::SearchEngine` (FTS5 + tag-filter + semantic lanes)            |
| `BM25_WEIGHT = 2.0`, `SEMANTIC_WEIGHT = 1.5`     | `pack_core::search::BM25_WEIGHT`, `SEMANTIC_WEIGHT`                               |
| Content-kind weight modifiers                    | `pack_core::search::TAG_BOOST_WEIGHT` (tag-intent boost; same role)               |
| `RankWeights` per-query override                 | `pack_core::search::RankWeights` (`Option<RankWeights>` on `SearchQuery`)         |
| Connection pragmas (`WAL`, `synchronous=NORMAL`) | `pack_core::schema::CONNECTION_PRAGMAS`                                           |
| f32 little-endian embedding blob                 | `pack_core::search::encode_f32_blob` / `decode_f32_blob`                          |

## Pipeline

```text
+-------------+     +------------+     +-----------+     +---------+
|  Crawlers   | --> |   Rights   | --> | Normalise | --> | Chunker |
| (17 source) |     |   Gate     |     |  -> .md   |     | speaker-|
+-------------+     +------------+     +-----------+     | turn    |
                          |                              +---------+
                          v                                    |
                  +-------------+                              v
                  | Governance  |                         +---------+
                  |   Log       |                         |  Tags   |
                  | (rejected)  |                         | 5-fam.  |
                  +-------------+                         +---------+
                                                               |
                                                               v
                              +-------------------------+
                              |   Metadata JSONL +      |
                              |   Chunks JSONL +        |
                              |   Governance JSONL      |
                              +-------------------------+
                                          |
                                          v
                              +-------------------------+
                              | pack_core::ingest       |
                              |  - BLAKE3 dedup         |
                              |  - FTS5 indexing        |
                              |  - schema triggers      |
                              +-------------------------+
                                          |
                                          v
                              +-------------------------+
                              | PackBuilder.build_to    |
                              |  - VACUUM INTO snapshot |
                              |  - zstd compress (L19)  |
                              |  - manifest + checksum  |
                              +-------------------------+
                                          |
                                          v
                                      .pack file
```

## Invariants

The three rules from the source-research document are enforced
machine-readably:

1. **Rights gate before chunking.** Implemented twice — once in
   the Python pipeline (`crawl/pipeline.py::_rights_gate_allows`)
   and once in the Rust store (`pack_core::ingest::PackStore::check_rights_gate`).
   Both share the same default allowlist; episodes outside the
   allowlist are recorded with `deprecated=true` and never
   chunked.

2. **Speaker-turn + section-heading chunking.** The Python
   `chunk_normalised_text` function detects ALLCAPS speaker
   labels (`SIMON LONDON: …`) and markdown headings, then
   produces 700-token chunks with 120-token overlap. Single
   monologues longer than the target are windowed by hard word
   stride.

3. **Companion-resource asset URLs.** Each crawler's
   `fetch_transcript` populates `RawEpisode.asset_urls` from
   PDF and external-domain anchor tags so chunks can deep-link
   to the referenced report / playbook / whitepaper.

## File layouts

### JSONL contracts

`packs/metadata/{publisher}.jsonl` — one line per episode:

```json
{
  "episode_id": "acquired_flagship_costco",
  "publisher": "acquired",
  "series": "flagship",
  "title": "Costco",
  "host": ["Ben Gilbert", "David Rosenthal"],
  "guest": [],
  "publication_date": "2021-09-21",
  "country_region": ["US", "global"],
  "industry_tags": ["retail"],
  "function_tags": ["strategy"],
  "business_model_tags": ["B2C"],
  "source_type": "podcast_transcript_html",
  "language": "en",
  "primary_url": "https://www.acquired.fm/episodes/costco",
  "asset_urls": [],
  "rights_code": "free_access_copyrighted",
  "rights_summary": "…",
  "credibility_notes": "…",
  "summary": "…",
  "chunking_policy": { "target_tokens": 700, "overlap_tokens": 120 }
}
```

`packs/chunks/{publisher}.jsonl` — one line per chunk:

```json
{
  "chunk_id": "acquired_flagship_costco#0007",
  "episode_id": "acquired_flagship_costco",
  "token_count": 688,
  "section_heading": "Membership Model",
  "chunk_text": "BEN: Costco's flywheel works because…",
  "citation_anchor": "https://www.acquired.fm/episodes/costco#membership-model"
}
```

`packs/governance/rights_log.jsonl` — append-only audit row:

```json
{
  "episode_id": "acquired_flagship_costco",
  "rights_code": "free_access_copyrighted",
  "ingestion_date": 1719360000,
  "content_hash": "blake3-hex",
  "deprecated": false
}
```

### `.pack` framing

```text
+-----------------------+
| MAGIC ("SNMDM")       |   5 bytes
+-----------------------+
| u8 pack_format_version|   1 byte
+-----------------------+
| u64 manifest_len      |   8 bytes, little-endian
+-----------------------+
|  MANIFEST (JSON)      |   manifest_len bytes
+-----------------------+
| u64 db_len            |   8 bytes
+-----------------------+
| zstd(sqlite db)       |   db_len bytes
+-----------------------+
```

The magic and version byte are split so a future v2 pack still
presents the `SNMDM` magic and the reader can surface a more
informative `UnsupportedPackVersion` error rather than a generic
`BadMagic`. Length headers are `u64` so packs are not silently
bounded to 4 GiB; the reader bounds-checks every header against
the input buffer and returns `TruncatedPack` rather than
panicking when a length exceeds the available bytes.

The manifest contains:

- `header.pack_format_version` (1)
- `header.schema_version` (1)
- `header.default_rank_weights` (BM25, semantic, tag-boost)
- `header.zstd_level` (default 19)
- `built_at` (epoch seconds)
- `publisher_count`, `episode_count`, `chunk_count`
- `blob_blake3` (verified on load)
- `publishers` (per-publisher counts)
- `build_notes` (git SHA + crawl date)

## SQLite schema (inside the pack)

See [`crates/pack_core/src/schema.rs`](../crates/pack_core/src/schema.rs)
for the canonical statements. The key invariants:

- `episode`, `chunk`, `governance_log` are **append-only** (triggers).
- `chunk.content_hash` is the BLAKE3 of the canonicalised chunk
  text (`canonicalise_text`: NFKC, normalised line endings, tabs
  expanded, trailing whitespace stripped).
- `chunk_fts` uses `tokenize='unicode61 remove_diacritics 2'`.
- `chunk_embeddings (chunk_id, model_tag)` lets the pack carry
  vectors for multiple model vintages without clobbering.

## Rank merge

`pack_core::search::SearchEngine::search` runs three lanes,
collects per-row contributions, sums them under the configured
weights, and sorts.

```
rank_score = bm25 * BM25_WEIGHT  (=2.0)
           + cosine * SEMANTIC_WEIGHT (=1.5)
           + tag_match * TAG_BOOST_WEIGHT (=0.75)
```

`BM25_WEIGHT` is applied to the **negated** SQLite bm25 score so
all lanes share the "higher is better" semantics. Per-lane
contributions are exposed on `SearchHit.bm25_score` /
`semantic_score` / `tag_match` to make the ranker debuggable
without re-running the query.
