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
/// * [`NapiError::Pack`] — forwarded error from
///   [`pack_core::PackError`]. The wrapper preserves the original
///   message and stamps its finer-grained ``kind`` on the wire.
#[derive(Debug, Clone, PartialEq, Eq, Error, Serialize, Deserialize)]
#[serde(tag = "kind", content = "detail")]
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
    pub fn kind(&self) -> &str {
        match self {
            Self::InvalidArgument { .. } => "InvalidArgument",
            Self::Internal { .. } => "Internal",
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
        Self::Pack {
            kind: "Io".to_string(),
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
        let err: NapiError =
            std::io::Error::new(std::io::ErrorKind::NotFound, "missing pack file").into();
        assert_eq!(err.kind(), "Io");
    }
}
