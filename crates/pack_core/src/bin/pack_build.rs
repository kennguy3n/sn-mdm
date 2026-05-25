//! `pack-build` — end-to-end pack assembly CLI.
//!
//! Reads the JSONL trees produced by `crawl/pipeline.py`
//! (`packs/metadata/*.jsonl`, `packs/chunks/*.jsonl`,
//! `packs/governance/rights_log.jsonl`) into an in-memory
//! [`PackStore`], applies the rights gate + content-hash dedup
//! invariants, and writes a compact `.pack` file via
//! [`PackBuilder::build_to`]. The output is the on-device deliverable
//! shipped to the agent (typically via a GitHub Release asset, not
//! committed to the repo).
//!
//! Usage:
//!
//! ```text
//! cargo run --release --bin pack-build -- \
//!     --packs-root packs \
//!     --output    packs/sn-mdm-tranche1.pack \
//!     --notes     "tranche1 $(git rev-parse --short HEAD) $(date -u +%FT%TZ)"
//! ```
//!
//! Exit codes:
//!
//! * `0` — pack written successfully.
//! * `1` — no metadata JSONL files were found under `--packs-root`.
//!   The crawl was never run, or wrote to a different
//!   directory.
//! * `2` — ingest or build failed.

use std::path::PathBuf;
use std::process::ExitCode;

use pack_core::{PackBuilder, PackStore};

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(msg) => {
            eprintln!("pack-build: {msg}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };

    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(BuildError::NoMetadata { path }) => {
            eprintln!(
                "pack-build: no metadata JSONL files under {}\n\
                 hint: did the crawl actually run? `python -m crawl.pipeline` first.",
                path.display()
            );
            ExitCode::from(1)
        }
        Err(BuildError::Pack(err)) => {
            eprintln!("pack-build: {err}");
            ExitCode::from(2)
        }
        Err(BuildError::Io(err)) => {
            eprintln!("pack-build: {err}");
            ExitCode::from(2)
        }
    }
}

#[derive(Debug)]
enum BuildError {
    NoMetadata { path: PathBuf },
    Pack(pack_core::PackError),
    Io(std::io::Error),
}

impl From<pack_core::PackError> for BuildError {
    fn from(value: pack_core::PackError) -> Self {
        BuildError::Pack(value)
    }
}

impl From<std::io::Error> for BuildError {
    fn from(value: std::io::Error) -> Self {
        BuildError::Io(value)
    }
}

fn run(args: &Args) -> Result<(), BuildError> {
    // Pre-flight: the JSONL trees must exist. Bailing here gives a
    // clearer error than letting `ingest_jsonl_files` silently
    // return an empty report and then watching `build_to` emit an
    // empty pack.
    let metadata_dir = args.packs_root.join("metadata");
    let any_metadata = metadata_dir.is_dir()
        && std::fs::read_dir(&metadata_dir)?
            .filter_map(|e| e.ok())
            .any(|e| e.path().extension().is_some_and(|x| x == "jsonl"));
    if !any_metadata {
        return Err(BuildError::NoMetadata {
            path: metadata_dir,
        });
    }

    eprintln!("pack-build: ingesting JSONL trees under {}", args.packs_root.display());
    let store = PackStore::open_in_memory()?;
    let report = store.ingest_jsonl_files(&args.packs_root)?;
    let totals = report.totals();

    eprintln!(
        "pack-build: ingest report — {} publishers, {} episodes seen, {} inserted, \
         {} rejected by rights gate, {} chunks seen, {} inserted, {} deduped, {} rights-skipped",
        report.by_publisher.len(),
        totals.episodes_seen,
        totals.episodes_inserted,
        totals.episodes_rejected_rights,
        totals.chunks_seen,
        totals.chunks_inserted,
        totals.chunks_skipped_dedup,
        totals.chunks_skipped_rights,
    );

    let mut builder = PackBuilder::new(&store);
    if let Some(notes) = &args.notes {
        builder = builder.with_build_notes(notes.clone());
    }

    eprintln!("pack-build: writing {} …", args.output.display());
    let manifest = builder.build_to(&args.output)?;

    eprintln!(
        "pack-build: wrote {} ({} publishers, {} episodes, {} chunks, format v{}, schema v{})",
        args.output.display(),
        manifest.publisher_count,
        manifest.episode_count,
        manifest.chunk_count,
        manifest.header.pack_format_version,
        manifest.header.schema_version,
    );
    Ok(())
}

#[derive(Debug)]
struct Args {
    packs_root: PathBuf,
    output: PathBuf,
    notes: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    // Hand-rolled flag parser. We deliberately avoid a `clap`
    // dependency for the CLI — keeping pack_core's runtime
    // dependency footprint as small as the spec calls for. The
    // surface is small (three flags) so a 30-line parser is fine.
    let mut packs_root: Option<PathBuf> = None;
    let mut output: Option<PathBuf> = None;
    let mut notes: Option<String> = None;
    let mut argv = std::env::args().skip(1);
    while let Some(flag) = argv.next() {
        match flag.as_str() {
            "--packs-root" => {
                packs_root = Some(PathBuf::from(
                    argv.next().ok_or("--packs-root needs a value")?,
                ));
            }
            "--output" | "-o" => {
                output = Some(PathBuf::from(argv.next().ok_or("--output needs a value")?));
            }
            "--notes" => {
                notes = Some(argv.next().ok_or("--notes needs a value")?);
            }
            "-h" | "--help" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(Args {
        packs_root: packs_root.ok_or("--packs-root is required")?,
        output: output.ok_or("--output is required")?,
        notes,
    })
}

const USAGE: &str = "Usage: pack-build --packs-root <DIR> --output <FILE> [--notes <STR>]

Ingest the JSONL outputs of `crawl/pipeline.py` and assemble a
compact .pack file via PackBuilder.

Arguments:
  --packs-root <DIR>   Root of the packs/ tree (expects metadata/,
                       chunks/, governance/ subdirectories).
  --output <FILE>      Output .pack path. Overwrites if it exists.
  --notes <STR>        Build notes stamped into the pack manifest
                       (typically a git rev + crawl date).
";
