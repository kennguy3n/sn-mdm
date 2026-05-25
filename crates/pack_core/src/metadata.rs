//! Metadata types for the **sn-mdm** content pack.
//!
//! These structs are the canonical Rust representation of the JSONL
//! lines emitted by the Python `crawl/` pipeline:
//!
//! * `packs/metadata/{publisher}.jsonl` — one [`Episode`] per line.
//! * `packs/chunks/{publisher}.jsonl`   — one [`Chunk`] per line.
//! * `packs/governance/rights_log.jsonl` — one [`GovernanceEntry`] per line.
//!
//! All types implement `serde::Serialize` + `serde::Deserialize` so
//! they round-trip through JSONL without manual conversion.
//!
//! The publisher / series / episode entity-relationship model
//! mirrors the research catalogue in `docs/SOURCES.md`. The
//! 5-family tag taxonomy ([`TagFamilies`]) is applied by the
//! crawler to every episode and is used both at ingest time (to
//! populate the structured-filter columns on the `episode` row)
//! and at query time (to power [`crate::search::SearchQuery::tags`]).

use serde::{Deserialize, Serialize};

/// Default chunking policy: 700 tokens with 120-token overlap, as
/// specified in the research. Crawlers may override via
/// [`ChunkingPolicy`] on a per-episode basis but the default is
/// applied when the JSONL line omits `chunking_policy`.
pub const DEFAULT_CHUNKING: ChunkingPolicy = ChunkingPolicy {
    target_tokens: 700,
    overlap_tokens: 120,
};

/// One row in the publisher dimension. Stored inline on the
/// [`Episode`] so the SQL schema does not need a separate
/// `publisher` table — packs are read-heavy and the join would be
/// the same JSON blob denormalised the other direction.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Publisher {
    /// Stable lower-snake-case identifier (e.g. `"acquired"`,
    /// `"bcg"`, `"ncsc_uk"`). Used as the JSONL filename prefix
    /// and as the first segment of every [`Episode::episode_id`].
    pub publisher_id: String,
    /// Display name (e.g. `"Acquired"`, `"BCG"`,
    /// `"UK National Cyber Security Centre"`).
    pub publisher_name: String,
    /// Country or region the publisher operates from
    /// (`"US"`, `"UK"`, `"Switzerland"`, `"global"`, …). Used in
    /// the `geography` tag family when a per-episode override is
    /// not supplied.
    pub country_region: String,
    /// Free-text source type — `"podcast_network"`,
    /// `"consultancy"`, `"government"`, …. Surfaces in
    /// `docs/SOURCES.md` as the row's category label.
    pub source_type: String,
}

/// A podcast / show / series under a [`Publisher`]. One publisher
/// may host multiple series (e.g. a16z runs the flagship `a16z`
/// podcast, `In the Vault`, `Bio Eats World`, …).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Series {
    /// Stable lower-snake-case identifier within the publisher
    /// (e.g. `"flagship"`, `"in_the_vault"`).
    pub series_id: String,
    /// Display title (e.g. `"In the Vault"`).
    pub series_title: String,
    /// Primary host name. Multi-host series record additional hosts
    /// on each individual [`Episode::host`].
    pub host: String,
    /// Canonical URL of the series hub page.
    pub primary_url: String,
}

/// Chunking policy attached to an [`Episode`]. The Python crawler
/// reads this off the source-registry TOML and embeds it on the
/// JSONL line so the consumer (the Rust `PackStore`) does not need
/// to re-read the registry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkingPolicy {
    /// Target chunk length in tokens. The Python chunker measures
    /// tokens with whitespace + punctuation splitting; this matches
    /// what an on-device tokenizer-free retriever measures, which
    /// is what the pack will be read by.
    pub target_tokens: usize,
    /// Number of tokens carried over from the previous chunk. A
    /// 120-token overlap on a 700-token target gives ~17% redundancy
    /// — enough to preserve cross-boundary references without
    /// blowing up the chunk count.
    pub overlap_tokens: usize,
}

impl Default for ChunkingPolicy {
    fn default() -> Self {
        DEFAULT_CHUNKING
    }
}

/// 5-family tag taxonomy applied to every episode at crawl time.
///
/// The taxonomy is **flat**: each family is a `Vec<String>` of
/// lower-case tags. The vocabulary is open-ended — crawlers may
/// emit new tags as new sources come online — but a controlled
/// vocabulary lives in `docs/SOURCES.md` and the source-registry
/// TOML to keep tag drift bounded.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TagFamilies {
    /// `product`, `marketing`, `HR`, `finance`, `banking`,
    /// `fintech`, `healthcare`, `legal`, `construction`,
    /// `retail`, `supply-chain`, `hospitality`, `real-estate`,
    /// `design-firm`, `startup`, …
    #[serde(default)]
    pub industry: Vec<String>,
    /// `strategy`, `pricing`, `GTM`, `performance-marketing`,
    /// `hiring`, `culture`, `M&A`, `governance`, `compliance`,
    /// `cyber-risk`, `operations`, `procurement`, …
    #[serde(default)]
    pub function: Vec<String>,
    /// `B2B`, `B2C`, `marketplace`, `SaaS`, `platform`,
    /// `regulated-incumbent`, `venture-backed`, `public-sector`, …
    #[serde(default)]
    pub business_model: Vec<String>,
    /// `US`, `UK`, `Canada`, `India`, `Singapore`, `Indonesia`,
    /// `GCC`, `Saudi Arabia`, `UAE`, `Switzerland`, `Germany`,
    /// `APAC`, `global`, …
    #[serde(default)]
    pub geography: Vec<String>,
    /// `full-transcript`, `transcript-pdf`, `show-notes`,
    /// `whitepaper`, `playbook`, `standards-guide`, `newsletter`,
    /// `webinar`, …
    #[serde(default)]
    pub evidence_type: Vec<String>,
}

/// One episode of one series. Serialised as one JSONL line in
/// `packs/metadata/{publisher}.jsonl`.
///
/// The schema matches the research catalogue's metadata fields
/// exactly. Optional fields default to empty vectors / strings via
/// `#[serde(default)]` so the Python crawler can omit them when
/// unknown without breaking the round-trip.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Episode {
    /// Stable, content-deterministic identifier. The canonical
    /// format is `"{publisher_id}_{series_id}_{episode_slug}"`
    /// (e.g. `"acquired_flagship_costco"`).
    pub episode_id: String,
    /// `Publisher::publisher_id` — denormalised onto the episode so
    /// `packs/metadata/{publisher}.jsonl` can be sharded by
    /// publisher without a join.
    pub publisher: String,
    /// `Series::series_id` — denormalised likewise.
    pub series: String,
    /// Episode title as published on the source.
    pub title: String,
    /// Host name(s) for this specific episode. May differ from
    /// `Series::host` for guest-hosted episodes.
    #[serde(default)]
    pub host: Vec<String>,
    /// Guest name(s) for this specific episode.
    #[serde(default)]
    pub guest: Vec<String>,
    /// ISO 8601 publication date (`YYYY-MM-DD` or full RFC 3339).
    pub publication_date: String,
    /// Geography tags applicable to this episode. Distinct from
    /// `TagFamilies::geography` because episodes can target a
    /// geography different from the publisher's (e.g. a BCG
    /// Switzerland-published episode about Saudi Arabia).
    #[serde(default)]
    pub country_region: Vec<String>,
    /// Industry tags. Mirrors `TagFamilies::industry`.
    #[serde(default)]
    pub industry_tags: Vec<String>,
    /// Function tags. Mirrors `TagFamilies::function`.
    #[serde(default)]
    pub function_tags: Vec<String>,
    /// Business-model tags. Mirrors `TagFamilies::business_model`.
    #[serde(default)]
    pub business_model_tags: Vec<String>,
    /// Source type — one of `podcast_transcript_pdf`,
    /// `podcast_transcript_html`, `show_notes`, `whitepaper`,
    /// `playbook`, `standards_guide`, `newsletter`, `webinar`.
    pub source_type: String,
    /// Primary language as a BCP-47 tag (`"en"`, `"en-US"`, `"de"`).
    pub language: String,
    /// Canonical URL of the episode landing page. This is the
    /// citation anchor used by chunks emitted from this episode.
    pub primary_url: String,
    /// Companion asset URLs detected during normalisation —
    /// reports, playbooks, whitepapers referenced inline by the
    /// transcript. Stored verbatim; the on-device agent can
    /// surface them as deep-link suggestions without re-resolving.
    #[serde(default)]
    pub asset_urls: Vec<String>,
    /// Rights code. The rights gate
    /// ([`crate::ingest::PackStore::ingest_episode`]) refuses to
    /// chunk any episode whose `rights_code` is not in the
    /// configured allowlist. Default allowlist is
    /// `["ogl_v3", "cc_by_nc_nd", "free_access_copyrighted",
    ///   "public_domain", "cc_by"]`.
    pub rights_code: String,
    /// Human-readable summary of the rights status — surfaced in
    /// the governance manifest and the export's `rights.json`.
    #[serde(default)]
    pub rights_summary: String,
    /// Free-text notes on the credibility of this source. Read by
    /// human reviewers; never used as a query input.
    #[serde(default)]
    pub credibility_notes: String,
    /// One-paragraph summary suitable for display next to a
    /// retrieval result.
    #[serde(default)]
    pub summary: String,
    /// Chunking policy in effect for this episode. Optional in the
    /// JSONL — when omitted the chunker uses [`DEFAULT_CHUNKING`].
    #[serde(default)]
    pub chunking_policy: Option<ChunkingPolicy>,
}

impl Episode {
    /// Return the chunking policy that was in effect, defaulting to
    /// [`DEFAULT_CHUNKING`] when the JSONL line omitted the field.
    pub fn effective_chunking(&self) -> ChunkingPolicy {
        self.chunking_policy.unwrap_or(DEFAULT_CHUNKING)
    }

    /// Aggregate this episode's `*_tags` columns and `country_region`
    /// into the 5-family taxonomy. Used by the indexer to populate
    /// the structured-filter columns on the `episode` row.
    pub fn tag_families(&self) -> TagFamilies {
        TagFamilies {
            industry: self.industry_tags.clone(),
            function: self.function_tags.clone(),
            business_model: self.business_model_tags.clone(),
            geography: self.country_region.clone(),
            // `evidence_type` is single-valued today but is modelled
            // as a vector so future episodes can carry both
            // `full-transcript` and `show-notes` when both are
            // crawled.
            evidence_type: vec![self.source_type.clone()],
        }
    }
}

/// One chunk emitted from one episode. Serialised as one JSONL
/// line in `packs/chunks/{publisher}.jsonl`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Chunk {
    /// Stable identifier. The canonical format is
    /// `"{episode_id}#{ordinal:04}"` (e.g.
    /// `"acquired_flagship_costco#0007"`).
    pub chunk_id: String,
    /// Owning episode's [`Episode::episode_id`].
    pub episode_id: String,
    /// Approximate token count (whitespace + punctuation split).
    pub token_count: usize,
    /// Section heading that started this chunk — `None` for chunks
    /// that begin mid-section.
    #[serde(default)]
    pub section_heading: Option<String>,
    /// Chunk text. For speaker-turn chunks the speaker label is
    /// preserved at the start of the chunk (e.g.
    /// `"SIMON LONDON: Welcome to …"`).
    pub chunk_text: String,
    /// Citation anchor. Always contains the episode's primary URL;
    /// when a section heading is available the anchor is appended
    /// (`"https://…/episode/foo#section-1"`).
    pub citation_anchor: String,
}

/// A row in the rights / governance dimension. The full controlled
/// vocabulary lives in the source registry; this struct is the
/// in-memory representation used by the governance log and the
/// `.pack` export manifest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RightsRecord {
    /// Lower-snake-case identifier, e.g. `"ogl_v3"`,
    /// `"cc_by_nc_nd"`, `"free_access_copyrighted"`.
    pub rights_code: String,
    /// One-paragraph human summary of the rights status.
    pub rights_summary: String,
    /// `"yes"` | `"no"` | `"with_attribution"` — does this rights
    /// code permit verbatim reuse?
    pub verbatim_reuse: String,
}

/// One row of the governance log
/// (`packs/governance/rights_log.jsonl`). Recorded for every
/// episode at ingest time so the pack ships with a complete
/// auditable trail.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GovernanceEntry {
    /// Episode whose ingestion this entry describes.
    pub episode_id: String,
    /// Rights code at ingest time. Matches the [`Episode::rights_code`]
    /// that flowed through the rights gate.
    pub rights_code: String,
    /// Ingest timestamp as Unix epoch seconds.
    pub ingestion_date: i64,
    /// BLAKE3 content hash of the canonical transcript text. Used
    /// to dedup re-crawls and to recognise content drift on the
    /// publisher side. Hex-encoded for JSONL friendliness.
    pub content_hash: String,
    /// `true` once this episode has been superseded by a newer
    /// version or removed from source. Flips append-only via a new
    /// log entry — historical rows are never rewritten.
    #[serde(default)]
    pub deprecated: bool,
}
