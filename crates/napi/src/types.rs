//! Wire-stable request types for the N-API bridge.
//!
//! The JS surface sends arbitrarily-shaped objects across the
//! boundary; napi-rs deserialises them into ``serde_json::Value``
//! and the binding layer ``from_value``s into these typed structs.
//! Keeping the shape in one place lets a JS host reflect on the
//! expected payload via the generated ``index.d.ts`` declarations
//! and avoids per-field parsing in [`super::bindings`].

use serde::{Deserialize, Serialize};

use pack_core::{SearchScope, TagFilter};

/// JS-side query shape for [`super::search`].
///
/// Every field is optional on the JS side; the defaults are set by
/// ``serde(default)`` so callers can send ``{}`` to mean "everything
/// default — pure FTS5 with no filter, limit 10".
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct QueryRequest {
    /// Free-text FTS5 query. Empty string disables the FTS lane.
    pub text: String,
    /// Tag filter. Each family is a flat ``Vec<String>``; serde-json
    /// renders that as a JS array of strings.
    pub tags: TagFilter,
    /// Maximum hits to return. ``0`` is treated as ``10`` by
    /// [`pack_core::SearchEngine::search`].
    pub limit: usize,
    /// Search scope. JS sends a string (``"local-only"`` /
    /// ``"include-embeddings"``).
    pub scope: SearchScope,
    /// Optional query embedding. Same dimension as the stored chunk
    /// vectors under [`Self::semantic_model_tag`]. Only consulted
    /// when [`Self::scope`] is
    /// [`pack_core::SearchScope::IncludeEmbeddings`].
    pub query_embedding: Option<Vec<f32>>,
    /// Model tag the [`Self::query_embedding`] was produced under.
    /// Required when ``query_embedding`` is present.
    pub semantic_model_tag: Option<String>,
}
