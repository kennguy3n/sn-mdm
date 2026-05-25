//! N-API bindings entry point.
//!
//! Exposes the substrate's pack-search surface as a Node.js native
//! addon via ``napi-rs`` ``#[napi]`` proc-macros. The pure-Rust
//! surface in [`crate`] is the canonical Rust-facing API; every
//! ``#[napi]``-annotated wrapper here is a thin adapter that:
//!
//! 1. Converts JS-side argument types (``BigInt`` handles, plain JS
//!    objects deserialised through ``serde_json::Value``) into the
//!    Rust types ([`crate::PackHandle`] is ``u64``;
//!    [`crate::QueryRequest`] is the typed payload).
//! 2. Forwards the call to the matching pure-Rust function.
//! 3. Maps any [`NapiError`] into a structured [`napi::Error`]
//!    whose ``reason`` is a JSON envelope
//!    ``{"kind": "...", "message": "...", "detail": {...}}`` so JS
//!    callers can switch on ``JSON.parse(e.message).kind``.
//!
//! The bindings are compiled in the ``cdylib`` artefact picked up
//! by ``napi build``. They are also reachable from Rust (via the
//! ``rlib`` crate-type) so unit tests can exercise the same
//! JSON-envelope error path that JavaScript callers see at runtime.
//!
//! ## Naming
//!
//! ``napi-derive`` automatically renames Rust ``snake_case``
//! identifiers to JS ``camelCase`` ones when generating the JS
//! surface — ``open_pack`` becomes ``openPack``, ``close_pack``
//! becomes ``closePack``, etc. See
//! <https://napi.rs/docs/concepts/values#naming>.

#![allow(clippy::needless_pass_by_value)]
// napi-derive hands owned values across the JS boundary on every
// call; borrowing would force an extra copy in the generated code.
#![allow(unsafe_code)]
// napi-derive's `#[napi]` proc-macro expands into FFI module-init
// stubs that necessarily touch raw C pointers (napi_env,
// napi_callback_info, napi_value). The expansion includes its own
// `#[allow(unsafe_code)]` on every generated `extern "C"` function;
// for any workspace-level `deny(unsafe_code)` to be overridable we
// mirror the allow here. The hand-written code in this module
// remains `unsafe`-free.

use napi::bindgen_prelude::{BigInt, Error as JsError, Result};
use napi_derive::napi;

use crate::types::JsSearchHit;
use crate::{JsQueryRequest, NapiError, NapiResult, PackHandle};

/// Convert a [`NapiError`] into a structured [`napi::Error`].
///
/// The JS-side ``Error.message`` is a JSON string of the form
/// ``{"kind":"BadMagic","message":"...","detail":{...}}`` where:
///
/// * ``kind`` is the flattened, finest-grained tag — for a wrapped
///   [`pack_core::PackError::BadMagic`] it is ``"BadMagic"``, not
///   ``"Pack"``. This is what JS callers switch on.
/// * ``message`` is the human-readable [`std::fmt::Display`]
///   string suitable for surfacing to the UI.
/// * ``detail`` is the full serialised [`NapiError`] envelope
///   (including the outer ``Pack`` wrapper) for telemetry that
///   needs the raw structure. The inner enum's serde tag is
///   ``variant`` (NOT ``kind``) so consumers walking ``detail``
///   for telemetry don't see two ``kind`` fields with different
///   meanings — the top-level ``kind`` is the finest-grained
///   pack-error tag; ``detail.variant`` is the NapiError outer
///   enum discriminant (``"Pack"`` / ``"InvalidArgument"`` /
///   ``"Internal"``).
///
/// Callers do:
///
/// ```js
/// try { packCore.openPack(path); }
/// catch (e) {
///   const env = JSON.parse(e.message);
///   if (env.kind === "BadMagic") { /* surface as bad-pack-file */ }
/// }
/// ```
///
/// We deliberately do NOT map kinds onto napi's fixed ``Status``
/// enum because that erases the finer-grained kind surface (every
/// caller bug would collapse onto ``InvalidArg``). The JSON
/// envelope preserves full fidelity.
fn to_js_error(err: NapiError) -> JsError {
    let envelope = serde_json::json!({
        "kind": err.kind(),
        "message": err.to_string(),
        "detail": serde_json::to_value(&err).unwrap_or(serde_json::Value::Null),
    });
    JsError::from_reason(envelope.to_string())
}

/// Convert a JS ``BigInt`` handle (as marshalled by ``napi-derive``)
/// into the substrate's [`PackHandle`].
///
/// JS represents [`PackHandle`] as a ``BigInt`` so the full 64-bit
/// width round-trips without precision loss (JS ``Number`` only
/// has 53 bits of mantissa). napi-rs' ``BigInt::get_u64()``
/// returns ``(sign, value, lossless)`` — we reject negative or
/// too-large values so an opaque host bug can't smuggle a
/// corrupted handle into the registry.
fn handle_from_bigint(handle: &BigInt) -> Result<PackHandle> {
    let (sign, value, lossless) = handle.get_u64();
    if sign {
        return Err(to_js_error(NapiError::InvalidArgument {
            message: "handle must be a non-negative BigInt".into(),
        }));
    }
    if !lossless {
        return Err(to_js_error(NapiError::InvalidArgument {
            message: "handle does not fit in a 64-bit unsigned integer".into(),
        }));
    }
    Ok(value)
}

/// Forward a [`NapiResult<T>`] into a napi [`Result<T>`].
fn forward<T>(result: NapiResult<T>) -> Result<T> {
    result.map_err(to_js_error)
}

/// Open and verify a ``.pack`` file at ``path``, returning a
/// ``BigInt`` handle the host must pass back into [`search`] and
/// [`close_pack`].
///
/// Mirrors [`crate::open_pack`].
///
/// # Errors
///
/// Surfaces the [`NapiError`] envelope on every failure path —
/// see [`crate::open_pack`] for the list.
#[napi(js_name = "openPack")]
pub fn open_pack(path: String) -> Result<BigInt> {
    let handle = forward(crate::open_pack(&path))?;
    Ok(BigInt::from(handle))
}

/// Run a query against the pack identified by ``handle``.
///
/// ``request`` is a plain JS object — see
/// [`crate::QueryRequest`] for the field shape. Returns a JS
/// array of plain objects mirroring [`pack_core::SearchHit`].
///
/// Mirrors [`crate::search`].
///
/// # Errors
///
/// Surfaces the [`NapiError`] envelope on every failure path —
/// see [`crate::search`] for the list. Additionally raises
/// [`NapiError::InvalidArgument`] when the JS ``request`` object
/// fails to deserialise into [`crate::QueryRequest`] (e.g. a
/// non-array ``tags.industry`` field, a non-number ``limit``).
#[napi(js_name = "search")]
pub fn search(handle: BigInt, request: serde_json::Value) -> Result<serde_json::Value> {
    let handle = handle_from_bigint(&handle)?;
    let typed: JsQueryRequest = serde_json::from_value(request).map_err(|err| {
        to_js_error(NapiError::InvalidArgument {
            message: format!("could not parse query request: {err}"),
        })
    })?;
    let hits = forward(crate::search(handle, typed))?;
    // Convert each ``pack_core::SearchHit`` into its camelCase
    // JS-facing twin before serialising. Keeping the conversion
    // explicit (rather than slapping ``rename_all`` on
    // ``SearchHit``) means the on-disk JSONL contract consumed by
    // the Python pipeline and the ``pack-search`` CLI stays
    // untouched while JS callers see idiomatic camelCase keys.
    let js_hits: Vec<JsSearchHit> = hits.into_iter().map(JsSearchHit::from).collect();
    serde_json::to_value(&js_hits).map_err(|err| {
        to_js_error(NapiError::Internal {
            message: format!("could not encode hits: {err}"),
        })
    })
}

/// Drop the open store identified by ``handle``. Returns ``true``
/// when an entry was removed, ``false`` for already-closed /
/// never-opened / sentinel handles. Idempotent.
///
/// Mirrors [`crate::close_pack`].
#[napi(js_name = "closePack")]
pub fn close_pack(handle: BigInt) -> Result<bool> {
    let handle = handle_from_bigint(&handle)?;
    Ok(crate::close_pack(handle))
}

#[cfg(test)]
mod tests {
    //! The tests here drive the same `#[napi]`-decorated wrappers
    //! Node calls through the cdylib — the ``rlib`` crate-type
    //! re-exposes them for Rust callers, so we get full coverage
    //! of the ``BigInt`` decode + JSON envelope + error mapping
    //! paths without spinning up a Node host.

    use super::*;

    #[test]
    fn handle_from_bigint_accepts_unsigned_value() {
        let bi = BigInt::from(42_u64);
        let handle = handle_from_bigint(&bi).expect("ok");
        assert_eq!(handle, 42);
    }

    #[test]
    fn handle_from_bigint_rejects_negative() {
        // ``BigInt::from(i64)`` with a negative value yields a
        // signed bigint; ``get_u64`` returns ``sign = true``.
        let bi = BigInt::from(-1_i64);
        let err = handle_from_bigint(&bi).expect_err("rejects negative");
        let envelope: serde_json::Value = serde_json::from_str(err.reason.as_str()).unwrap();
        assert_eq!(envelope["kind"], "InvalidArgument");
    }

    #[test]
    fn to_js_error_envelope_includes_kind_and_message() {
        let err = NapiError::InvalidArgument {
            message: "bad handle".into(),
        };
        let js = to_js_error(err);
        let parsed: serde_json::Value = serde_json::from_str(js.reason.as_str()).unwrap();
        assert_eq!(parsed["kind"], "InvalidArgument");
        assert!(parsed["message"].as_str().unwrap().contains("bad handle"));
        // ``detail`` carries the NapiError enum's serde tag under
        // ``variant`` (deliberately not ``kind``, to avoid a name
        // collision with the top-level flattened ``kind``).
        assert_eq!(parsed["detail"]["variant"], "InvalidArgument");
        assert!(
            parsed["detail"].get("kind").is_none(),
            "detail must not have a 'kind' field alongside top-level kind"
        );
    }

    #[test]
    fn to_js_error_forwards_pack_kind() {
        let err: NapiError = pack_core::PackError::BadMagic.into();
        let js = to_js_error(err);
        let parsed: serde_json::Value = serde_json::from_str(js.reason.as_str()).unwrap();
        // ``kind`` is the inner pack-error tag, not ``"Pack"``.
        assert_eq!(parsed["kind"], "BadMagic");
    }

    #[test]
    fn search_with_zero_bigint_handle_is_rejected() {
        // ``0n`` is the reserved sentinel. The binding-level
        // ``handle_from_bigint`` accepts it (zero is a valid
        // unsigned integer), so the rejection happens inside
        // ``crate::search``.
        let err = search(BigInt::from(0_u64), serde_json::json!({})).expect_err("must reject");
        let parsed: serde_json::Value = serde_json::from_str(err.reason.as_str()).unwrap();
        assert_eq!(parsed["kind"], "InvalidArgument");
    }

    #[test]
    fn close_pack_unknown_handle_returns_false() {
        // ``999_999`` was never minted in this test process —
        // ``close_pack`` should report ``false`` rather than
        // raise.
        let removed = close_pack(BigInt::from(999_999_u64)).expect("no error");
        assert!(!removed);
    }

    #[test]
    fn search_malformed_request_surfaces_invalid_argument() {
        // ``tags`` as a string instead of an object: cannot
        // deserialise into the typed ``QueryRequest``.
        let bad = serde_json::json!({ "tags": "industry" });
        let err = search(BigInt::from(1_u64), bad).expect_err("must reject");
        let parsed: serde_json::Value = serde_json::from_str(err.reason.as_str()).unwrap();
        assert_eq!(parsed["kind"], "InvalidArgument");
    }
}
