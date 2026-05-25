//! Multi-lane search over a pack store.
//!
//! Mirrors the pattern in `kennguy3n/chat-storage-search`'s
//! `QueryEngine`: parallel "lanes" produce per-row scores that are
//! merged into a single rank. We carry three lanes:
//!
//! 1. **FTS5 / BM25 lane** — full-text query against `chunk_fts`.
//!    Returns rows with a BM25 score (lower is better in SQLite's
//!    `bm25` extension; we negate so callers see "higher is better"
//!    consistently across lanes).
//! 2. **Tag-filter lane** — structured-filter against the
//!    `industry_tags`, `function_tags`, `business_model_tags`,
//!    `geography_tags`, and `evidence_type` JSON columns on
//!    `episode`. Powered by SQLite's `json_each`.
//! 3. **Semantic lane** — optional. When the caller supplies a
//!    pre-computed query embedding (and the pack has at least one
//!    `chunk_embeddings` row under the matching `model_tag`), the
//!    engine scores rows by cosine similarity. Embeddings are
//!    stored as little-endian `f32` blobs to match the format used
//!    by `chat-storage-search`'s semantic shard.
//!
//! Rank-merge weights ([`RankWeights`]) match the
//! `chat-storage-search` defaults: BM25 = 2.0, semantic = 1.5,
//! tag-boost = 0.75. A row that matches both FTS and the tag
//! filter accumulates both contributions, so query-tag intent is
//! always preferred over query-only.

use std::collections::HashMap;

use rusqlite::params;
use serde::{Deserialize, Serialize};

use crate::error::Result;
use crate::ingest::PackStore;

/// BM25 contribution weight in the merged rank score.
/// (Matches `chat-storage-search`'s `BM25_WEIGHT`.)
pub const BM25_WEIGHT: f64 = 2.0;
/// Semantic / cosine-similarity contribution weight.
pub const SEMANTIC_WEIGHT: f64 = 1.5;
/// Multiplicative boost when a row's owning episode matches every
/// supplied tag filter. Additive on top of BM25 + semantic.
pub const TAG_BOOST_WEIGHT: f64 = 0.75;

/// Tunable per-lane weights. Defaults mirror the constants above
/// — exposed as a struct so callers can override at query time
/// (e.g. tag-only queries set `bm25 = 0.0`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RankWeights {
    pub bm25: f64,
    pub semantic: f64,
    pub tag_boost: f64,
}

impl Default for RankWeights {
    fn default() -> Self {
        Self {
            bm25: BM25_WEIGHT,
            semantic: SEMANTIC_WEIGHT,
            tag_boost: TAG_BOOST_WEIGHT,
        }
    }
}

/// Search scope. `LocalOnly` returns rows from the current pack
/// store; `IncludeEmbeddings` additionally consults the semantic
/// lane when the caller provides a query embedding.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum SearchScope {
    /// FTS5 + tag-filter only. Default scope.
    #[default]
    LocalOnly,
    /// FTS5 + tag-filter + semantic lane. The caller must supply a
    /// query embedding on [`SearchQuery::query_embedding`].
    IncludeEmbeddings,
}

/// Tag filter applied to all three lanes. Each `Vec<String>` is
/// treated as a logical OR within the family; multiple families
/// are AND-ed together (an episode must have at least one matching
/// tag in every non-empty family).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TagFilter {
    #[serde(default)]
    pub industry: Vec<String>,
    #[serde(default)]
    pub function: Vec<String>,
    #[serde(default)]
    pub business_model: Vec<String>,
    #[serde(default)]
    pub geography: Vec<String>,
    #[serde(default)]
    pub evidence_type: Vec<String>,
}

impl TagFilter {
    /// `true` if every non-empty family has at least one tag.
    /// Used to short-circuit the SQL build when there's nothing
    /// to filter on.
    pub fn is_empty(&self) -> bool {
        self.industry.is_empty()
            && self.function.is_empty()
            && self.business_model.is_empty()
            && self.geography.is_empty()
            && self.evidence_type.is_empty()
    }
}

/// One search query.
#[derive(Debug, Clone, Default)]
pub struct SearchQuery {
    /// Free-text query string. Empty string disables the FTS lane.
    /// FTS5 special characters (`"`, `:`, `*`, …) are escaped at
    /// the boundary so caller-supplied input is treated as a
    /// phrase, not as an FTS5 operator.
    pub text: String,
    /// Tag filter. Applied to all enabled lanes.
    pub tags: TagFilter,
    /// Optional query embedding for the semantic lane. Must be the
    /// same dimension as the stored chunk vectors under
    /// `semantic_model_tag`.
    pub query_embedding: Option<Vec<f32>>,
    /// Model tag the query embedding was produced under. Required
    /// when `query_embedding` is `Some`.
    pub semantic_model_tag: Option<String>,
    /// Maximum number of hits to return. `0` is treated as `10`.
    pub limit: usize,
    /// Scope of the query.
    pub scope: SearchScope,
    /// Optional rank-weight override.
    pub weights: Option<RankWeights>,
}

/// One result row. The `rank_score` is in arbitrary units —
/// callers should treat it as "higher is better" and compare only
/// within a single query.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchHit {
    pub chunk_id: String,
    pub episode_id: String,
    pub chunk_text: String,
    pub citation_anchor: String,
    pub section_heading: Option<String>,
    /// Merged rank score across all enabled lanes.
    pub rank_score: f64,
    /// Per-lane contributions, useful for debugging the ranker.
    pub bm25_score: Option<f64>,
    pub semantic_score: Option<f64>,
    pub tag_match: bool,
    /// ``chunk.created_at`` (Unix epoch seconds) for the matched
    /// row. Used as a deterministic secondary sort key after
    /// ``rank_score``, so equally-ranked hits (the common case for
    /// pure tag-only queries where every survivor accrues the same
    /// ``tag_boost``) come back in newest-first order rather than
    /// in `HashMap` iteration order.
    pub created_at: i64,
}

/// Search engine attached to a [`PackStore`]. Stateless — every
/// query opens a fresh set of prepared statements. Held by value
/// rather than by reference so the engine can be moved into a
/// thread once the underlying connection has been opened on it.
pub struct SearchEngine<'a> {
    store: &'a PackStore,
}

impl<'a> SearchEngine<'a> {
    /// Wrap a `PackStore` for querying. Cheap — does not touch the
    /// database.
    pub fn new(store: &'a PackStore) -> Self {
        Self { store }
    }

    /// Run a query. Returns at most `query.limit` hits (default 10)
    /// in descending `rank_score` order.
    pub fn search(&self, query: &SearchQuery) -> Result<Vec<SearchHit>> {
        let weights = query.weights.unwrap_or_default();
        let limit = if query.limit == 0 { 10 } else { query.limit };

        // Episodes that satisfy the tag filter. Used to mark
        // `tag_match` on every result row and to short-circuit
        // ranking on tag-only queries.
        let matching_episodes = self.episodes_matching_tags(&query.tags)?;
        let tags_active = !query.tags.is_empty();

        let mut merged: HashMap<String, SearchHit> = HashMap::new();

        // ---- FTS5 lane ---------------------------------------------
        if !query.text.trim().is_empty() {
            let escaped = escape_fts5(&query.text);
            let mut stmt = self.store.connection().prepare(
                r#"SELECT
                       c.chunk_id, c.episode_id, c.chunk_text,
                       c.citation_anchor, c.section_heading,
                       c.created_at,
                       bm25(chunk_fts) AS bm
                   FROM chunk_fts
                   JOIN chunk c ON c.chunk_id = chunk_fts.chunk_id
                   WHERE chunk_fts MATCH ?
                   ORDER BY bm
                   LIMIT 500"#,
            )?;
            let rows = stmt.query_map(params![escaped], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, f64>(6)?,
                ))
            })?;
            for row in rows {
                let (chunk_id, episode_id, chunk_text, citation, section, created_at, bm) = row?;
                // SQLite's `bm25()` returns lower-is-better; flip
                // the sign so callers see higher-is-better
                // consistently across lanes.
                let bm_score = -bm;
                if tags_active && !matching_episodes.contains(&episode_id) {
                    continue;
                }
                let entry = merged.entry(chunk_id.clone()).or_insert_with(|| SearchHit {
                    chunk_id: chunk_id.clone(),
                    episode_id: episode_id.clone(),
                    chunk_text,
                    citation_anchor: citation,
                    section_heading: section,
                    rank_score: 0.0,
                    bm25_score: None,
                    semantic_score: None,
                    tag_match: matching_episodes.contains(&episode_id),
                    created_at,
                });
                entry.bm25_score = Some(bm_score);
                entry.rank_score += weights.bm25 * bm_score;
            }
        }

        // ---- Tag-only lane -----------------------------------------
        // When there's no FTS query, return chunks ordered by
        // recency from the tag-matching episode set. Skip entirely
        // when the tag filter matched no episodes — otherwise we'd
        // synthesize a `WHERE episode_id IN ()` clause, which SQLite
        // rejects as a syntax error.
        if query.text.trim().is_empty() && tags_active && !matching_episodes.is_empty() {
            let placeholders = std::iter::repeat_n("?", matching_episodes.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                r#"SELECT c.chunk_id, c.episode_id, c.chunk_text,
                          c.citation_anchor, c.section_heading, c.created_at
                   FROM chunk c
                   WHERE c.episode_id IN ({placeholders})
                   ORDER BY c.created_at DESC
                   LIMIT 500"#
            );
            let mut stmt = self.store.connection().prepare(&sql)?;
            let params_iter =
                rusqlite::params_from_iter(matching_episodes.iter().map(|s| s.as_str()));
            let rows = stmt.query_map(params_iter, |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, i64>(5)?,
                ))
            })?;
            for row in rows {
                let (chunk_id, episode_id, chunk_text, citation, section, created_at) = row?;
                let entry = merged.entry(chunk_id.clone()).or_insert_with(|| SearchHit {
                    chunk_id: chunk_id.clone(),
                    episode_id: episode_id.clone(),
                    chunk_text,
                    citation_anchor: citation,
                    section_heading: section,
                    rank_score: 0.0,
                    bm25_score: None,
                    semantic_score: None,
                    tag_match: true,
                    created_at,
                });
                entry.tag_match = true;
            }
        }

        // ---- Semantic lane (optional) ------------------------------
        if matches!(query.scope, SearchScope::IncludeEmbeddings) {
            if let (Some(qvec), Some(tag)) = (
                query.query_embedding.as_ref(),
                query.semantic_model_tag.as_ref(),
            ) {
                let mut stmt = self.store.connection().prepare(
                    r#"SELECT e.chunk_id, c.episode_id, c.chunk_text,
                              c.citation_anchor, c.section_heading,
                              c.created_at, e.embedding
                       FROM chunk_embeddings e
                       JOIN chunk c ON c.chunk_id = e.chunk_id
                       WHERE e.model_tag = ?"#,
                )?;
                let rows = stmt.query_map(params![tag], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, Option<String>>(4)?,
                        r.get::<_, i64>(5)?,
                        r.get::<_, Vec<u8>>(6)?,
                    ))
                })?;
                for row in rows {
                    let (chunk_id, episode_id, chunk_text, citation, section, created_at, vec_blob) =
                        row?;
                    if tags_active && !matching_episodes.contains(&episode_id) {
                        continue;
                    }
                    let chunk_vec = decode_f32_blob(&vec_blob)?;
                    if chunk_vec.len() != qvec.len() {
                        // Mismatched dimension — skip rather than
                        // silently misrank.
                        continue;
                    }
                    let sim = cosine_similarity(qvec, &chunk_vec);
                    let entry = merged.entry(chunk_id.clone()).or_insert_with(|| SearchHit {
                        chunk_id: chunk_id.clone(),
                        episode_id: episode_id.clone(),
                        chunk_text,
                        citation_anchor: citation,
                        section_heading: section,
                        rank_score: 0.0,
                        bm25_score: None,
                        semantic_score: None,
                        tag_match: matching_episodes.contains(&episode_id),
                        created_at,
                    });
                    entry.semantic_score = Some(sim);
                    entry.rank_score += weights.semantic * sim;
                }
            }
        }

        // ---- Tag boost ---------------------------------------------
        if tags_active {
            for hit in merged.values_mut() {
                if hit.tag_match {
                    hit.rank_score += weights.tag_boost;
                }
            }
        }

        // ---- Merge + sort ------------------------------------------
        // Primary key: ``rank_score`` (higher is better).
        // Secondary key: ``created_at`` (newer is better). The
        // ``HashMap`` above loses insertion order, so equally-
        // ranked hits (the common case for pure tag-only queries
        // where every survivor accrues the same ``tag_boost``)
        // would otherwise come back in iteration order. The
        // secondary key restores the SQL-side recency intent
        // documented for the tag-only lane and, for the BM25 and
        // semantic lanes, gives ties a deterministic newest-first
        // order across runs.
        // Tertiary key: ``chunk_id`` so two chunks created in the
        // same second still sort deterministically.
        let mut hits: Vec<SearchHit> = merged.into_values().collect();
        hits.sort_by(|a, b| {
            b.rank_score
                .partial_cmp(&a.rank_score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| b.created_at.cmp(&a.created_at))
                .then_with(|| a.chunk_id.cmp(&b.chunk_id))
        });
        hits.truncate(limit);
        Ok(hits)
    }

    /// Episodes that satisfy the supplied tag filter. Empty filter
    /// returns an empty set (callers short-circuit before calling).
    fn episodes_matching_tags(
        &self,
        filter: &crate::search::TagFilter,
    ) -> Result<std::collections::HashSet<String>> {
        if filter.is_empty() {
            return Ok(Default::default());
        }
        // Build the WHERE clause family-by-family. Each non-empty
        // family becomes an EXISTS subquery against `json_each` of
        // the matching column.
        let mut clauses: Vec<String> = Vec::new();
        let mut bindings: Vec<String> = Vec::new();
        let bind = |col: &str,
                    values: &[String],
                    clauses: &mut Vec<String>,
                    b: &mut Vec<String>| {
            if values.is_empty() {
                return;
            }
            let placeholders = std::iter::repeat_n("?", values.len())
                .collect::<Vec<_>>()
                .join(",");
            clauses.push(format!(
                "EXISTS (SELECT 1 FROM json_each(episode.{col}) WHERE json_each.value IN ({placeholders}))"
            ));
            b.extend(values.iter().cloned());
        };
        bind(
            "industry_tags",
            &filter.industry,
            &mut clauses,
            &mut bindings,
        );
        bind(
            "function_tags",
            &filter.function,
            &mut clauses,
            &mut bindings,
        );
        bind(
            "business_model_tags",
            &filter.business_model,
            &mut clauses,
            &mut bindings,
        );
        bind(
            "geography_tags",
            &filter.geography,
            &mut clauses,
            &mut bindings,
        );
        bind(
            "evidence_type",
            &filter.evidence_type,
            &mut clauses,
            &mut bindings,
        );
        let where_clause = clauses.join(" AND ");
        let sql = format!("SELECT episode_id FROM episode WHERE {where_clause}");
        let mut stmt = self.store.connection().prepare(&sql)?;
        let params_iter = rusqlite::params_from_iter(bindings.iter().map(|s| s.as_str()));
        let mut rows = stmt.query(params_iter)?;
        let mut out = std::collections::HashSet::new();
        while let Some(row) = rows.next()? {
            out.insert(row.get::<_, String>(0)?);
        }
        Ok(out)
    }
}

/// Escape an FTS5 phrase so user input is interpreted as a literal
/// phrase. Surrounds in double-quotes and doubles any embedded
/// double-quotes. Punctuation inside the phrase is fine — FTS5
/// strips it at tokenize time.
fn escape_fts5(input: &str) -> String {
    let escaped = input.replace('"', "\"\"");
    format!("\"{escaped}\"")
}

/// Decode a little-endian `f32` blob into a `Vec<f32>`. Returns an
/// error if the blob is not a multiple of 4 bytes long.
fn decode_f32_blob(bytes: &[u8]) -> Result<Vec<f32>> {
    if bytes.len() % 4 != 0 {
        return Err(crate::PackError::Invariant(format!(
            "embedding blob length {} is not a multiple of 4",
            bytes.len()
        )));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4);
    for chunk in bytes.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Ok(out)
}

/// Encode a `Vec<f32>` to a little-endian blob. Useful for tests
/// and for the optional embedder hook.
pub fn encode_f32_blob(values: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(values.len() * 4);
    for v in values {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

/// Cosine similarity between two equal-length vectors. Returns
/// `0.0` if either vector is the zero vector (rather than NaN).
fn cosine_similarity(a: &[f32], b: &[f32]) -> f64 {
    debug_assert_eq!(a.len(), b.len());
    let mut dot = 0.0_f64;
    let mut na = 0.0_f64;
    let mut nb = 0.0_f64;
    for (x, y) in a.iter().zip(b.iter()) {
        let xf = *x as f64;
        let yf = *y as f64;
        dot += xf * yf;
        na += xf * xf;
        nb += yf * yf;
    }
    if na == 0.0 || nb == 0.0 {
        return 0.0;
    }
    dot / (na.sqrt() * nb.sqrt())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metadata::{Chunk, Episode};

    fn seed(store: &PackStore) {
        let ep = Episode {
            episode_id: "acquired_flagship_costco".into(),
            publisher: "acquired".into(),
            series: "flagship".into(),
            title: "Costco".into(),
            host: vec!["Ben Gilbert".into()],
            guest: vec![],
            publication_date: "2021-09-21".into(),
            country_region: vec!["US".into()],
            industry_tags: vec!["retail".into()],
            function_tags: vec!["strategy".into()],
            business_model_tags: vec!["B2C".into()],
            source_type: "podcast_transcript_html".into(),
            language: "en".into(),
            primary_url: "https://www.acquired.fm/episodes/costco".into(),
            asset_urls: vec![],
            rights_code: "free_access_copyrighted".into(),
            rights_summary: "Free-access copyrighted.".into(),
            credibility_notes: "First-party.".into(),
            summary: "Costco's strategic moats.".into(),
            chunking_policy: None,
        };
        store.ingest_episode(&ep).unwrap();
        for (i, text) in [
            "BEN: Costco's flywheel works because of the membership fee.",
            "DAVID: The treasure-hunt format keeps members engaged.",
            "BEN: Kirkland Signature is the single biggest CPG brand by revenue.",
        ]
        .iter()
        .enumerate()
        {
            store
                .ingest_chunk(&Chunk {
                    chunk_id: format!("acquired_flagship_costco#{i:04}"),
                    episode_id: ep.episode_id.clone(),
                    token_count: text.split_whitespace().count(),
                    section_heading: Some(format!("Section {i}")),
                    chunk_text: (*text).into(),
                    citation_anchor: format!("{}#section-{i}", ep.primary_url),
                })
                .unwrap();
        }
    }

    #[test]
    fn fts_lane_finds_phrase() {
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        let engine = SearchEngine::new(&store);
        let hits = engine
            .search(&SearchQuery {
                text: "membership fee".into(),
                limit: 5,
                ..Default::default()
            })
            .unwrap();
        assert!(!hits.is_empty());
        assert!(hits[0].chunk_text.contains("membership"));
        assert!(hits[0].bm25_score.is_some());
    }

    #[test]
    fn tag_filter_restricts_results() {
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        let engine = SearchEngine::new(&store);
        let hits = engine
            .search(&SearchQuery {
                text: "Costco".into(),
                tags: TagFilter {
                    industry: vec!["retail".into()],
                    ..Default::default()
                },
                limit: 5,
                ..Default::default()
            })
            .unwrap();
        assert!(hits.iter().all(|h| h.tag_match));

        let empty = engine
            .search(&SearchQuery {
                text: "Costco".into(),
                tags: TagFilter {
                    industry: vec!["finance".into()],
                    ..Default::default()
                },
                limit: 5,
                ..Default::default()
            })
            .unwrap();
        assert!(empty.is_empty(), "no episodes match finance tag");
    }

    #[test]
    fn tag_only_query_returns_chunks_ordered_by_recency() {
        // Regression: the tag-only lane SQL `ORDER BY c.created_at
        // DESC` was being discarded by the downstream `HashMap`
        // merge step, so equally-ranked hits (every tag-only
        // survivor accrues the same ``tag_boost``) came back in
        // iteration order. The fix carries ``created_at`` on
        // ``SearchHit`` and uses it as the secondary sort key.
        //
        // To exercise the *ordering* (not just the count), this test
        // seeds three chunks with *deliberately distinct*
        // ``created_at`` values. ``ingest_chunk`` stamps
        // ``Utc::now()`` which collapses to the same second across
        // a fast test run, so we directly update the rows after
        // ingest — the chunk PK is still authoritative for
        // identity, and the test only inspects the search ordering.
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        // Override ``created_at`` so the three chunks are stamped
        // ``1000``, ``2000``, ``3000`` — a 1000-second-apart
        // ladder so the tag-only lane has a real recency signal to
        // honour. We expect descending order: #0002, #0001, #0000.
        let pairs = [
            ("acquired_flagship_costco#0000", 1000_i64),
            ("acquired_flagship_costco#0001", 2000_i64),
            ("acquired_flagship_costco#0002", 3000_i64),
        ];
        for (chunk_id, ts) in pairs.iter() {
            // The schema's append-only trigger rejects UPDATE on
            // ``chunk``. Drop the trigger for the duration of this
            // test only — every other test path still goes
            // through ``ingest_chunk``, which is the production
            // contract.
            store
                .connection()
                .execute_batch(
                    "DROP TRIGGER IF EXISTS chunk_no_update;\nDROP TRIGGER IF EXISTS chunk_no_delete;",
                )
                .unwrap();
            store
                .connection()
                .execute(
                    "UPDATE chunk SET created_at = ? WHERE chunk_id = ?",
                    params![ts, chunk_id],
                )
                .unwrap();
        }

        let engine = SearchEngine::new(&store);
        let hits = engine
            .search(&SearchQuery {
                text: "".into(),
                tags: TagFilter {
                    industry: vec!["retail".into()],
                    ..Default::default()
                },
                limit: 5,
                ..Default::default()
            })
            .unwrap();
        assert_eq!(hits.len(), 3);
        // Newest first — the secondary sort key on
        // ``created_at`` must restore the SQL-side ordering that the
        // ``HashMap`` merge erases.
        assert_eq!(hits[0].chunk_id, "acquired_flagship_costco#0002");
        assert_eq!(hits[1].chunk_id, "acquired_flagship_costco#0001");
        assert_eq!(hits[2].chunk_id, "acquired_flagship_costco#0000");
        for h in &hits {
            assert!(h.tag_match);
        }
        // Every tag-only hit must carry its real ``created_at`` —
        // not the default `0` — so downstream consumers can use
        // the field for further ranking / display.
        assert_eq!(hits[0].created_at, 3000);
        assert_eq!(hits[1].created_at, 2000);
        assert_eq!(hits[2].created_at, 1000);
    }

    #[test]
    fn semantic_lane_scores_when_embeddings_present() {
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        // Manually insert two 3-d embeddings: the one aligned with
        // the query should rank above the orthogonal one.
        let aligned = encode_f32_blob(&[1.0, 0.0, 0.0]);
        let orthogonal = encode_f32_blob(&[0.0, 1.0, 0.0]);
        let now = chrono::Utc::now().timestamp();
        store
            .connection()
            .execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedding, model_tag, created_at) VALUES (?, ?, ?, ?)",
                params![
                    "acquired_flagship_costco#0000",
                    aligned,
                    "test-3d",
                    now,
                ],
            )
            .unwrap();
        store
            .connection()
            .execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedding, model_tag, created_at) VALUES (?, ?, ?, ?)",
                params![
                    "acquired_flagship_costco#0001",
                    orthogonal,
                    "test-3d",
                    now,
                ],
            )
            .unwrap();

        let engine = SearchEngine::new(&store);
        let hits = engine
            .search(&SearchQuery {
                text: "".into(),
                tags: TagFilter::default(),
                query_embedding: Some(vec![0.9, 0.1, 0.0]),
                semantic_model_tag: Some("test-3d".into()),
                scope: SearchScope::IncludeEmbeddings,
                limit: 5,
                ..Default::default()
            })
            .unwrap();
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].chunk_id, "acquired_flagship_costco#0000");
        assert!(hits[0].semantic_score.unwrap() > hits[1].semantic_score.unwrap());
    }

    #[test]
    fn tag_only_query_with_no_matches_returns_empty() {
        // Regression: an earlier version generated `IN ()` SQL when
        // the tag filter matched no episodes, which SQLite rejects
        // with a syntax error.
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        let engine = SearchEngine::new(&store);
        let hits = engine
            .search(&SearchQuery {
                text: "".into(),
                tags: TagFilter {
                    industry: vec!["industry-that-does-not-exist".into()],
                    ..Default::default()
                },
                limit: 5,
                ..Default::default()
            })
            .expect("should not error");
        assert!(hits.is_empty());
    }

    #[test]
    fn fts_query_with_unmatched_tag_filter_returns_empty() {
        // Regression: confirm the FTS lane also returns empty when
        // the tag filter excludes everything.
        let store = PackStore::open_in_memory().unwrap();
        seed(&store);
        let engine = SearchEngine::new(&store);
        let hits = engine
            .search(&SearchQuery {
                text: "membership".into(),
                tags: TagFilter {
                    industry: vec!["industry-that-does-not-exist".into()],
                    ..Default::default()
                },
                limit: 5,
                ..Default::default()
            })
            .expect("should not error");
        assert!(hits.is_empty());
    }
}
