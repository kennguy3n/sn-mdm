//! Wire-stable request and response types for the N-API bridge.
//!
//! The JS surface sends arbitrarily-shaped objects across the
//! boundary; napi-rs deserialises them into ``serde_json::Value``
//! and the binding layer ``from_value``s into these typed structs.
//! Keeping the shape in one place lets a JS host reflect on the
//! expected payload via the generated ``index.d.ts`` declarations
//! and avoids per-field parsing in [`super::bindings`].
//!
//! ## Naming
//!
//! Every type in this module — *including nested ones* — uses
//! ``rename_all = "camelCase"`` and ``rename_all = "kebab-case"``
//! (for enums) so the entire JS-facing surface is consistent. The
//! corresponding `pack_core` types use Rust's natural snake_case
//! and are mapped through ``From`` impls below. We do not propagate
//! the ``rename_all`` attribute back into `pack_core` because that
//! struct's serde shape is part of the on-disk JSONL contract
//! consumed by the Python pipeline and the ``pack-search`` CLI.

use serde::{Deserialize, Serialize};

use pack_core::{SearchHit, SearchScope, TagFilter};

/// JS-side tag filter mirroring [`pack_core::TagFilter`] with
/// camelCase field names matching the rest of the JS surface.
///
/// All families are optional on the JS side via
/// ``#[serde(default)]`` so a partial filter like
/// ``{ industry: ["retail"] }`` round-trips cleanly. Empty
/// families behave the same as omitting them.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct JsTagFilter {
    /// Industry-family tag set.
    pub industry: Vec<String>,
    /// Function-family tag set.
    pub function: Vec<String>,
    /// Business-model-family tag set.
    pub business_model: Vec<String>,
    /// Geography-family tag set.
    pub geography: Vec<String>,
    /// Evidence-type-family tag set.
    pub evidence_type: Vec<String>,
}

impl From<JsTagFilter> for TagFilter {
    fn from(js: JsTagFilter) -> Self {
        TagFilter {
            industry: js.industry,
            function: js.function,
            business_model: js.business_model,
            geography: js.geography,
            evidence_type: js.evidence_type,
        }
    }
}

impl From<TagFilter> for JsTagFilter {
    fn from(t: TagFilter) -> Self {
        JsTagFilter {
            industry: t.industry,
            function: t.function,
            business_model: t.business_model,
            geography: t.geography,
            evidence_type: t.evidence_type,
        }
    }
}

/// JS-side scope mirroring [`pack_core::SearchScope`].
///
/// Serialises to / from kebab-case strings so JS callers send
/// human-readable values (``"local-only"`` /
/// ``"include-embeddings"``) rather than the Rust enum's natural
/// PascalCase. This matches typical Node.js conventions for enum
/// strings and keeps the JS surface idiomatic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum JsSearchScope {
    /// FTS5 + tag-filter only. Default scope on the JS side; matches
    /// [`pack_core::SearchScope::LocalOnly`].
    #[default]
    LocalOnly,
    /// FTS5 + tag-filter + semantic lane; the caller must supply a
    /// query embedding via [`JsQueryRequest::query_embedding`].
    /// Matches [`pack_core::SearchScope::IncludeEmbeddings`].
    IncludeEmbeddings,
}

impl From<JsSearchScope> for SearchScope {
    fn from(js: JsSearchScope) -> Self {
        match js {
            JsSearchScope::LocalOnly => SearchScope::LocalOnly,
            JsSearchScope::IncludeEmbeddings => SearchScope::IncludeEmbeddings,
        }
    }
}

impl From<SearchScope> for JsSearchScope {
    fn from(s: SearchScope) -> Self {
        match s {
            SearchScope::LocalOnly => JsSearchScope::LocalOnly,
            SearchScope::IncludeEmbeddings => JsSearchScope::IncludeEmbeddings,
        }
    }
}

/// JS-side query shape for [`super::search`].
///
/// Every field is optional on the JS side; the defaults are set by
/// ``serde(default)`` so callers can send ``{}`` to mean "everything
/// default — pure FTS5 with no filter, limit 10".
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct JsQueryRequest {
    /// Free-text FTS5 query. Empty string disables the FTS lane.
    pub text: String,
    /// Tag filter. Each family is a flat ``Vec<String>``; serde-json
    /// renders that as a JS array of strings.
    pub tags: JsTagFilter,
    /// Maximum hits to return. ``0`` is treated as ``10`` by
    /// [`pack_core::SearchEngine::search`].
    pub limit: usize,
    /// Search scope. JS sends a kebab-case string
    /// (``"local-only"`` / ``"include-embeddings"``).
    pub scope: JsSearchScope,
    /// Optional query embedding. Same dimension as the stored chunk
    /// vectors under [`Self::semantic_model_tag`]. Only consulted
    /// when [`Self::scope`] is [`JsSearchScope::IncludeEmbeddings`].
    pub query_embedding: Option<Vec<f32>>,
    /// Model tag the [`Self::query_embedding`] was produced under.
    /// Required when ``query_embedding`` is present.
    pub semantic_model_tag: Option<String>,
}

/// Back-compat alias preserved for callers that imported the
/// pre-rename type. New code should use [`JsQueryRequest`].
pub type QueryRequest = JsQueryRequest;

/// JS-side hit row mirroring [`pack_core::SearchHit`] with all
/// fields renamed to camelCase. Constructed via the [`From`] impl
/// below so [`super::search`] can return JS-idiomatic JSON without
/// touching the canonical `pack_core` type.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct JsSearchHit {
    /// Stable chunk identifier (``{episode_id}#{0001}``).
    pub chunk_id: String,
    /// Episode the chunk belongs to.
    pub episode_id: String,
    /// Verbatim chunk text after canonicalisation.
    pub chunk_text: String,
    /// Citation URL or anchor preserved from ingestion.
    pub citation_anchor: String,
    /// Optional section heading captured during chunking.
    pub section_heading: Option<String>,
    /// Merged rank score across all enabled lanes. Higher is
    /// better; only meaningful within a single query.
    pub rank_score: f64,
    /// BM25 lane contribution if FTS fired for this row.
    pub bm25_score: Option<f64>,
    /// Semantic lane contribution if the row came back from the
    /// embeddings scan.
    pub semantic_score: Option<f64>,
    /// True when the row was admitted by the tag-filter lane.
    pub tag_match: bool,
    /// ``chunk.created_at`` (Unix epoch seconds) — secondary sort
    /// key after ``rank_score``.
    pub created_at: i64,
}

impl From<SearchHit> for JsSearchHit {
    fn from(h: SearchHit) -> Self {
        JsSearchHit {
            chunk_id: h.chunk_id,
            episode_id: h.episode_id,
            chunk_text: h.chunk_text,
            citation_anchor: h.citation_anchor,
            section_heading: h.section_heading,
            rank_score: h.rank_score,
            bm25_score: h.bm25_score,
            semantic_score: h.semantic_score,
            tag_match: h.tag_match,
            created_at: h.created_at,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_filter_round_trips_camel_case() {
        let json = serde_json::json!({
            "industry": ["retail"],
            "businessModel": ["B2B"],
            "evidenceType": ["case_study"],
        });
        let typed: JsTagFilter = serde_json::from_value(json).expect("decode");
        assert_eq!(typed.industry, vec!["retail"]);
        assert_eq!(typed.business_model, vec!["B2B"]);
        assert_eq!(typed.evidence_type, vec!["case_study"]);
        // Round-trip back through the core type to make sure the
        // From impl preserves the values.
        let core: TagFilter = typed.clone().into();
        assert_eq!(core.business_model, vec!["B2B"]);
        let again: JsTagFilter = core.into();
        assert_eq!(again.business_model, typed.business_model);
    }

    #[test]
    fn tag_filter_rejects_snake_case_business_model() {
        // The JS surface is camelCase-only; sending
        // ``business_model`` on the wire must NOT silently bind to
        // ``business_model`` (that was the original BUG_0001).
        let json = serde_json::json!({
            "business_model": ["B2B"],
        });
        let typed: JsTagFilter = serde_json::from_value(json).expect("decode");
        // The snake_case key is treated as unknown and dropped —
        // the camelCase-only field stays empty. We do NOT silently
        // accept snake_case, because that was the silent-data-loss
        // path the original bug warned about.
        assert!(typed.business_model.is_empty());
    }

    #[test]
    fn search_scope_serialises_as_kebab_case() {
        assert_eq!(
            serde_json::to_value(JsSearchScope::LocalOnly).unwrap(),
            serde_json::Value::String("local-only".into()),
        );
        assert_eq!(
            serde_json::to_value(JsSearchScope::IncludeEmbeddings).unwrap(),
            serde_json::Value::String("include-embeddings".into()),
        );
        let s: JsSearchScope = serde_json::from_str("\"include-embeddings\"").unwrap();
        assert_eq!(s, JsSearchScope::IncludeEmbeddings);
    }

    #[test]
    fn search_hit_serialises_camel_case() {
        let core = SearchHit {
            chunk_id: "abc#1".into(),
            episode_id: "abc".into(),
            chunk_text: "hello".into(),
            citation_anchor: "https://example.com#1".into(),
            section_heading: Some("Heading".into()),
            rank_score: 1.5,
            bm25_score: Some(0.75),
            semantic_score: None,
            tag_match: true,
            created_at: 1_700_000_000,
        };
        let js: JsSearchHit = core.into();
        let value = serde_json::to_value(&js).expect("serialise");
        assert_eq!(value["chunkId"], "abc#1");
        assert_eq!(value["episodeId"], "abc");
        assert_eq!(value["rankScore"], 1.5);
        assert_eq!(value["bm25Score"], 0.75);
        assert!(value["semanticScore"].is_null());
        assert_eq!(value["tagMatch"], true);
        assert_eq!(value["citationAnchor"], "https://example.com#1");
        assert_eq!(value["sectionHeading"], "Heading");
        assert_eq!(value["createdAt"], 1_700_000_000);
        // No snake_case sneak-through.
        assert!(value.get("chunk_id").is_none());
        assert!(value.get("rank_score").is_none());
    }

    #[test]
    fn query_request_round_trips_camel_case() {
        let json = serde_json::json!({
            "text": "hello",
            "tags": { "industry": ["x"], "businessModel": ["y"] },
            "limit": 7,
            "scope": "include-embeddings",
            "queryEmbedding": [0.1_f32, 0.2_f32],
            "semanticModelTag": "miniLM-v1",
        });
        let typed: JsQueryRequest = serde_json::from_value(json).expect("decode");
        assert_eq!(typed.text, "hello");
        assert_eq!(typed.tags.business_model, vec!["y"]);
        assert_eq!(typed.limit, 7);
        assert_eq!(typed.scope, JsSearchScope::IncludeEmbeddings);
        assert_eq!(
            typed.query_embedding.as_deref(),
            Some(&[0.1_f32, 0.2_f32][..])
        );
        assert_eq!(typed.semantic_model_tag.as_deref(), Some("miniLM-v1"));
    }
}
