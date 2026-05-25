//! N-API build script.
//!
//! ``napi_build::setup()`` is the canonical entry point documented at
//! <https://napi.rs/docs/cli/build>. It emits the platform-specific
//! linker directives the Node.js loader needs to ``dlopen`` (or
//! ``LoadLibrary`` on Windows) the ``.node`` artefact:
//!
//! * On Windows it injects a ``/EXPORT:napi_register_module_v1``
//!   pragma so the Node loader can resolve the module-init symbol.
//! * On macOS it sets ``-undefined dynamic_lookup`` so unresolved
//!   ``napi_*`` symbols are deferred to the embedding Node process.
//! * On Linux this is a no-op.
//!
//! Without this build step the cdylib still compiles, but
//! ``require()`` from Node would fail at load time with
//! ``dlopen: undefined symbol`` on macOS / ``LNK2019`` on Windows.
fn main() {
    napi_build::setup();
}
