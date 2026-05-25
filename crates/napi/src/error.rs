//! JSON-stable error envelope for the N-API bridge.
//!
//! The bridge serialises every failure into a structured JSON
//! payload so JavaScript callers can switch on ``kind`` instead of
//! parsing a free-form ``Error.message`` string. The discriminant
//! tag is finest-grained: a wrapped [`pack_core::PackError::BadMagic`]
//! surfaces as ``kind = "BadMagic"``, not ``kind = "Pack"``.
//!
//! See [`super::bindings`] for how this type is mapped onto a
//! [`napi::Error`].

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Result alias used by the pure-Rust facade exposed from
/// [`super`].
pub type NapiResult<T> = std::result::Result<T, NapiError>;

/// JSON-stable error envelope.
///
/// Variants:
///
/// * [`NapiError::InvalidArgument`] — JS caller passed a bad value
///   (unknown handle, malformed path, non-positive limit, ...).
/// * [`NapiError::Internal`] — substrate-side packaging step failed
///   **after** the underlying call succeeded (e.g. ``serde_json``
///   encoding of a result struct). Tagged ``Internal`` so host
///   telemetry can distinguish "caller bug" from "substrate bug"
///   without pattern-matching on opaque strings.
/// * [`NapiError::Io`] — filesystem error originating from the
///   substrate itself (e.g. tempdir creation in [`super::open_pack`]),
///   **not** from a ``pack_core`` call. Kept distinct from
///   [`NapiError::Pack`] so telemetry can tell apart "the substrate
///   could not stage a tempdir" from "the pack content was bad" —
///   the two have different remediation paths (transient OS issue
///   vs. corrupt input).
/// * [`NapiError::Pack`] — forwarded error from
///   [`pack_core::PackError`]. The wrapper preserves the original
///   message and stamps its finer-grained ``kind`` on the wire.
///   A wrapped [`pack_core::PackError::Io`] still surfaces here with
///   ``kind = "Io"`` because the IO failed inside pack_core's own
///   plumbing — the ``variant`` tag (``Pack`` vs ``Io``) carries
///   the source-of-error provenance.
/// ## Wire shape
///
/// Serialised via ``#[serde(tag = "variant", content = "data")]`` so
/// the envelope at the JS boundary has a single ``kind`` field at
/// the top level (flattened by [`super::bindings::to_js_error`]).
/// The inner ``variant`` tag is deliberately distinct from
/// ``kind`` so consumers do not see two fields with the same name
/// and different meanings when they walk ``detail`` for telemetry.
#[derive(Debug, Clone, PartialEq, Eq, Error, Serialize, Deserialize)]
#[serde(tag = "variant", content = "data")]
pub enum NapiError {
    /// A JS-side argument failed validation.
    #[error("invalid argument: {message}")]
    InvalidArgument {
        /// Diagnostic for the host.
        message: String,
    },

    /// A substrate-side encoding / packaging step failed *after*
    /// the underlying ``pack_core`` call already returned a value.
    /// Currently raised only by [`super::search`] when serialising
    /// a [`pack_core::SearchHit`] into a ``serde_json::Value``.
    #[error("internal: {message}")]
    Internal {
        /// Diagnostic for the host.
        message: String,
    },

    /// Filesystem error originating from the substrate — distinct
    /// from [`NapiError::Pack`] because the IO failed inside the
    /// bridge's own staging code (tempdir creation, extract path)
    /// rather than inside a ``pack_core`` call. The wire ``kind`` is
    /// still ``"Io"`` so JS callers can match a single tag for
    /// "any filesystem problem", and the ``variant`` discriminant
    /// in ``detail`` (``"Io"`` here, ``"Pack"`` if it came from
    /// pack_core) reveals which side of the bridge the error
    /// originated on.
    #[error("io: {message}")]
    Io {
        /// Original [`std::fmt::Display`] text from the
        /// [`std::io::Error`].
        message: String,
    },

    /// Forwarded error from the underlying [`pack_core`] surface.
    /// Carries the [`pack_core::PackError`] message string —
    /// [`pack_core::PackError`] itself is not ``Serialize``, so we
    /// can't carry the structured variant across the wire; the
    /// message + a stable ``kind`` tag is enough for host code to
    /// route on.
    #[error("pack error ({kind}): {message}")]
    Pack {
        /// Stable kind tag mirroring the [`pack_core::PackError`]
        /// variant.
        kind: String,
        /// Original [`std::fmt::Display`] text from
        /// [`pack_core::PackError`].
        message: String,
    },
}

impl NapiError {
    /// Discriminant tag for stable wire matching. Forwards the
    /// [`NapiError::Pack`] sub-tag so host code can ``switch`` on
    /// ``"BadMagic"`` / ``"ChecksumMismatch"`` / ... directly
    /// instead of having to unwrap a generic ``"Pack"`` envelope.
    /// [`NapiError::Io`] surfaces as the literal ``"Io"`` tag, the
    /// same tag a ``pack_core``-originated IO error carries via the
    /// ``Pack`` variant — JS hosts that only care "is this a
    /// filesystem problem?" can switch on a single string.
    pub fn kind(&self) -> &str {
        match self {
            Self::InvalidArgument { .. } => "InvalidArgument",
            Self::Internal { .. } => "Internal",
            Self::Io { .. } => "Io",
            Self::Pack { kind, .. } => kind.as_str(),
        }
    }
}

impl From<pack_core::PackError> for NapiError {
    fn from(value: pack_core::PackError) -> Self {
        Self::Pack {
            kind: pack_error_kind(&value).to_string(),
            message: value.to_string(),
        }
    }
}

impl From<std::io::Error> for NapiError {
    fn from(value: std::io::Error) -> Self {
        // Substrate-side IO (tempdir creation, extract path) is
        // its own variant so telemetry can tell "the OS couldn't
        // stage a temp file" from "the pack content was bad".
        // A pack_core IO error is wrapped via the
        // [`From<pack_core::PackError>`] impl above instead, which
        // routes through the ``Pack`` variant with ``kind = "Io"``.
        Self::Io {
            message: value.to_string(),
        }
    }
}

/// Stable kind tag for each [`pack_core::PackError`] variant. The
/// match is exhaustive so a new variant in ``pack_core`` will fail
/// to compile this crate, forcing a deliberate decision about the
/// wire tag rather than a silent ``"Unknown"`` fallback.
fn pack_error_kind(err: &pack_core::PackError) -> &'static str {
    use pack_core::PackError::*;
    match err {
        Sqlite(_) => "Sqlite",
        Json(_) => "Json",
        Io(_) => "Io",
        IncompatibleSchema { .. } => "IncompatibleSchema",
        BadMagic => "BadMagic",
        UnsupportedPackVersion { .. } => "UnsupportedPackVersion",
        TruncatedPack { .. } => "TruncatedPack",
        ChecksumMismatch { .. } => "ChecksumMismatch",
        RightsGateRefused { .. } => "RightsGateRefused",
        Invariant(_) => "Invariant",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_argument_round_trips() {
        let err = NapiError::InvalidArgument {
            message: "handle is not open".into(),
        };
        let s = serde_json::to_string(&err).unwrap();
        let back: NapiError = serde_json::from_str(&s).unwrap();
        assert_eq!(err, back);
        assert_eq!(err.kind(), "InvalidArgument");
    }

    #[test]
    fn internal_round_trips() {
        let err = NapiError::Internal {
            message: "could not encode hits".into(),
        };
        let s = serde_json::to_string(&err).unwrap();
        let back: NapiError = serde_json::from_str(&s).unwrap();
        assert_eq!(err, back);
        assert_eq!(err.kind(), "Internal");
    }

    #[test]
    fn pack_bad_magic_forwards_kind() {
        let err: NapiError = pack_core::PackError::BadMagic.into();
        assert_eq!(err.kind(), "BadMagic");
        assert!(err.to_string().contains("BadMagic"));
    }

    #[test]
    fn pack_checksum_mismatch_forwards_kind() {
        let err: NapiError = pack_core::PackError::ChecksumMismatch {
            claimed: "a".into(),
            computed: "b".into(),
        }
        .into();
        assert_eq!(err.kind(), "ChecksumMismatch");
    }

    #[test]
    fn pack_truncated_forwards_kind() {
        let err: NapiError = pack_core::PackError::TruncatedPack {
            field: "header",
            offset: 0,
            needed: 22,
            available: 4,
        }
        .into();
        assert_eq!(err.kind(), "TruncatedPack");
    }

    #[test]
    fn io_forwards_io_kind() {
        // ``std::io::Error`` -> ``NapiError::Io`` (substrate-side IO).
        let err: NapiError =
            std::io::Error::new(std::io::ErrorKind::NotFound, "missing pack file").into();
        assert_eq!(err.kind(), "Io");
        // ``variant`` discriminant is the new ``Io`` variant, not
        // ``Pack`` — so JS callers walking ``detail.variant`` for
        // source-of-error provenance see the right side of the
        // bridge.
        let v: serde_json::Value = serde_json::to_value(&err).unwrap();
        assert_eq!(v["variant"], "Io");
        assert_eq!(v["data"]["message"], "missing pack file");
    }

    #[test]
    fn pack_core_io_still_routes_through_pack_variant() {
        // A ``pack_core::PackError::Io`` is still a ``Pack``
        // envelope (the IO failed inside pack_core, not in the
        // substrate). The ``kind`` tag is the same ``"Io"`` so
        // single-tag JS handlers keep working, but the ``variant``
        // discriminant differs so detailed handlers can route
        // by source-of-error.
        let inner = std::io::Error::other("sqlite vfs failed");
        let err: NapiError = pack_core::PackError::Io(inner).into();
        assert_eq!(err.kind(), "Io");
        let v: serde_json::Value = serde_json::to_value(&err).unwrap();
        assert_eq!(v["variant"], "Pack");
        assert_eq!(v["data"]["kind"], "Io");
    }

    #[test]
    fn io_round_trips() {
        let err = NapiError::Io {
            message: "could not stage tempdir".into(),
        };
        let s = serde_json::to_string(&err).unwrap();
        let back: NapiError = serde_json::from_str(&s).unwrap();
        assert_eq!(err, back);
        assert_eq!(err.kind(), "Io");
    }

    #[test]
    fn serde_inner_tag_is_variant_not_kind() {
        // The inner serde tag is ``variant`` so the JSON envelope
        // assembled by ``bindings::to_js_error`` does not have two
        // ``kind`` fields with different meanings. The top-level
        // ``kind`` is the finest-grained pack-error tag; this inner
        // ``variant`` is the NapiError-enum discriminant.
        let err: NapiError = pack_core::PackError::BadMagic.into();
        let v: serde_json::Value = serde_json::to_value(&err).unwrap();
        assert_eq!(v["variant"], "Pack", "inner tag must be 'variant'");
        assert_eq!(v["data"]["kind"], "BadMagic");
        assert!(v.get("kind").is_none(), "inner serde must NOT add 'kind'");
    }
}
