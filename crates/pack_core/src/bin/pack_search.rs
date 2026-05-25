//! `pack-search` — shell-facing search CLI over a `.pack` file.
//!
//! Mirrors the architecture of `pack-build`: hand-rolled flag parser,
//! no external CLI crate, three exit codes (`0` success, `1`
//! caller-input error, `2` pack-load or query error). The binary
//! opens a [`PackReader`], decompresses the embedded SQLite blob into
//! a tempdir, re-opens it as a [`PackStore`], wraps it with a
//! [`SearchEngine`], and emits results to stdout.
//!
//! Two output modes:
//!
//! * `--format json` (default) — newline-separated JSON object per
//!   hit. Easy to pipe into `jq` or feed to a downstream tool.
//! * `--format text` — human-readable summary, one block per hit,
//!   with citation anchor + section heading + rank components. Good
//!   for ad-hoc shell use.
//!
//! Usage:
//!
//! ```text
//! cargo run --release --bin pack-search -- \
//!     --pack  packs/sn-mdm-tranche1.pack \
//!     --query "supply chain pivot" \
//!     --limit 10 \
//!     --format json
//! ```
//!
//! Tag filters are flat comma-separated lists per family:
//!
//! ```text
//! pack-search --pack pack.bin --tags-industry technology,fintech \
//!                             --tags-function operations \
//!                             --tags-evidence-type case_study
//! ```
//!
//! All tag flags AND together across families and OR together within
//! a family — matching the semantics of
//! [`pack_core::search::TagFilter`].
//!
//! Tag-only queries (no `--query`) are supported: a tag filter with
//! no FTS text exercises the structured-match lane only. Ordering
//! falls back to `created_at DESC` then `chunk_id ASC` so identical
//! `tag_boost` rows return deterministically newest-first.
//!
//! Exit codes:
//!
//! * `0` — search ran and produced results (possibly empty).
//! * `1` — caller-input error: missing `--pack`, malformed flag,
//!   `--limit` out of range, `--format` not in `{json, text}`.
//! * `2` — pack-load / query-execution error: the pack failed
//!   header / checksum / schema validation, the temp extract path
//!   failed, or `SearchEngine::search` returned a [`PackError`].

use std::path::PathBuf;
use std::process::ExitCode;

use pack_core::{
    PackError, PackReader, PackStore, SearchEngine, SearchHit, SearchQuery, TagFilter,
};

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(msg) => {
            eprintln!("pack-search: {msg}\n\n{USAGE}");
            return ExitCode::from(1);
        }
    };

    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(SearchCliError::Io(err)) => {
            eprintln!("pack-search: {err}");
            ExitCode::from(2)
        }
        Err(SearchCliError::Pack(err)) => {
            eprintln!("pack-search: {err}");
            ExitCode::from(2)
        }
    }
}

#[derive(Debug)]
enum SearchCliError {
    Pack(PackError),
    Io(std::io::Error),
}

impl From<PackError> for SearchCliError {
    fn from(value: PackError) -> Self {
        SearchCliError::Pack(value)
    }
}

impl From<std::io::Error> for SearchCliError {
    fn from(value: std::io::Error) -> Self {
        SearchCliError::Io(value)
    }
}

fn run(args: &Args) -> Result<(), SearchCliError> {
    // Open + verify the pack (header + checksum + schema), then
    // decompress the SQLite blob into a private tempdir.
    let reader = PackReader::open(&args.pack)?;
    eprintln!(
        "pack-search: opened {} ({} publishers, {} episodes, {} chunks, format v{}, schema v{})",
        args.pack.display(),
        reader.manifest.publisher_count,
        reader.manifest.episode_count,
        reader.manifest.chunk_count,
        reader.manifest.header.pack_format_version,
        reader.manifest.header.schema_version,
    );

    // ``tempfile::Builder`` produces a unique directory in the OS
    // temp dir. The `_dir` binding is kept alive for the rest of
    // `run` so the dir (and its contents) is cleaned up when the
    // function returns — including on the error paths.
    let dir = tempfile::Builder::new().prefix("pack-search-").tempdir()?;
    let db_path = dir.path().join("pack.sqlite");
    reader.extract_to(&db_path)?;

    // The pack format ships an unencrypted SQLite blob (the
    // VACUUM INTO inside `PackBuilder` writes a plaintext copy of
    // the in-memory store), so opening with an empty passphrase is
    // the correct invocation. Future encrypted-pack support would
    // surface a `--passphrase` flag.
    let store = PackStore::open(&db_path, "")?;
    let engine = SearchEngine::new(&store);

    let query = SearchQuery {
        text: args.query.clone().unwrap_or_default(),
        tags: args.tags.clone(),
        query_embedding: None,
        semantic_model_tag: None,
        limit: args.limit,
        scope: Default::default(),
        weights: None,
    };

    let hits = engine.search(&query)?;
    emit(&hits, args.format);
    Ok(())
}

fn emit(hits: &[SearchHit], format: OutputFormat) {
    match format {
        OutputFormat::Json => {
            // Newline-separated JSON: each hit on its own line.
            // Easier to pipe through `jq -c` than a single array.
            for hit in hits {
                match serde_json::to_string(hit) {
                    Ok(line) => println!("{line}"),
                    Err(err) => {
                        // SearchHit is plain `Serialize` with no
                        // failure paths — serialisation only fails
                        // on programmer error (e.g. a Map with
                        // non-string keys), so a runtime panic here
                        // would be appropriate. We log instead so
                        // the rest of the run keeps producing
                        // output for the caller.
                        eprintln!("pack-search: failed to serialise hit: {err}");
                    }
                }
            }
        }
        OutputFormat::Text => {
            if hits.is_empty() {
                println!("(no results)");
                return;
            }
            for (idx, hit) in hits.iter().enumerate() {
                let section = hit.section_heading.as_deref().unwrap_or("—");
                let bm25 = hit
                    .bm25_score
                    .map(|v| format!("{v:.3}"))
                    .unwrap_or_else(|| "—".to_string());
                let sem = hit
                    .semantic_score
                    .map(|v| format!("{v:.3}"))
                    .unwrap_or_else(|| "—".to_string());
                let tag = if hit.tag_match { "yes" } else { "no" };
                println!(
                    "[{i}] {citation} (rank={rank:.3}, bm25={bm25}, semantic={sem}, tag_match={tag})\n\
                     section: {section}\n\
                     episode: {episode}\n\
                     {text}\n",
                    i = idx + 1,
                    citation = hit.citation_anchor,
                    rank = hit.rank_score,
                    episode = hit.episode_id,
                    text = truncate(&hit.chunk_text, 280),
                );
            }
        }
    }
}

/// Trim long chunk bodies for the text emitter. JSON output keeps
/// the full body. ``…`` is appended when truncation actually occurs
/// so the consumer can tell the body is clipped.
fn truncate(text: &str, max: usize) -> String {
    // Operate on char count (not byte count) so we never split a
    // UTF-8 grapheme. Podcast transcripts are largely ASCII but
    // smart quotes / em-dashes / accented names appear regularly.
    let mut iter = text.chars();
    let head: String = iter.by_ref().take(max).collect();
    if iter.next().is_some() {
        format!("{head}…")
    } else {
        head
    }
}

#[derive(Debug, Clone, Copy)]
enum OutputFormat {
    Json,
    Text,
}

#[derive(Debug)]
struct Args {
    pack: PathBuf,
    query: Option<String>,
    tags: TagFilter,
    limit: usize,
    format: OutputFormat,
}

fn parse_args() -> Result<Args, String> {
    let mut pack: Option<PathBuf> = None;
    let mut query: Option<String> = None;
    let mut tags = TagFilter::default();
    let mut limit: usize = 10;
    let mut format = OutputFormat::Json;
    let mut argv = std::env::args().skip(1);
    while let Some(flag) = argv.next() {
        match flag.as_str() {
            "--pack" => pack = Some(PathBuf::from(value(&mut argv, &flag)?)),
            "--query" => query = Some(value(&mut argv, &flag)?),
            "--tags-industry" => tags.industry = parse_csv(&value(&mut argv, &flag)?),
            "--tags-function" => tags.function = parse_csv(&value(&mut argv, &flag)?),
            "--tags-business-model" => tags.business_model = parse_csv(&value(&mut argv, &flag)?),
            "--tags-geography" => tags.geography = parse_csv(&value(&mut argv, &flag)?),
            "--tags-evidence-type" => tags.evidence_type = parse_csv(&value(&mut argv, &flag)?),
            "--limit" => {
                let raw = value(&mut argv, &flag)?;
                let n: usize = raw
                    .parse()
                    .map_err(|_| format!("--limit must be a positive integer, got {raw:?}"))?;
                if n == 0 {
                    return Err("--limit must be > 0".into());
                }
                limit = n;
            }
            "--format" => {
                let raw = value(&mut argv, &flag)?;
                format = match raw.as_str() {
                    "json" => OutputFormat::Json,
                    "text" => OutputFormat::Text,
                    other => {
                        return Err(format!(
                            "--format must be one of `json` or `text`, got {other:?}"
                        ))
                    }
                };
            }
            "--help" | "-h" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            other => return Err(format!("unknown flag {other:?}")),
        }
    }
    let pack = pack.ok_or_else(|| "missing required flag --pack".to_string())?;
    if query.is_none() && tags.is_empty() {
        return Err(
            "must supply at least one of --query or --tags-* (otherwise the search is empty)"
                .into(),
        );
    }
    Ok(Args {
        pack,
        query,
        tags,
        limit,
        format,
    })
}

fn value(argv: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
    argv.next()
        .ok_or_else(|| format!("flag {flag} requires a value"))
}

fn parse_csv(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

const USAGE: &str = "Usage: pack-search --pack <path> [--query <text>] [--tags-* <csv>] \
                     [--limit <n>] [--format json|text]

Required:
  --pack <path>                Path to a `.pack` file produced by pack-build.

Query (supply at least one of these):
  --query <text>               Free-text FTS5 query. Treated as a phrase;
                               FTS5 punctuation is escaped at the boundary.
  --tags-industry <a,b,c>      OR-within-family, AND-across-families.
  --tags-function <a,b,c>
  --tags-business-model <a,b,c>
  --tags-geography <a,b,c>
  --tags-evidence-type <a,b,c>

Output:
  --limit <n>                  Maximum hits to return. Default 10.
  --format <json|text>         Output mode. Default json.

  -h, --help                   Show this message and exit.

Exit codes:
  0 — search ran (possibly empty result set).
  1 — caller-input error (missing/bad flag).
  2 — pack-load or query-execution error.";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_csv_strips_whitespace_and_drops_empties() {
        // The CLI accepts shell-friendly forms like
        // `--tags-industry "fintech, ai , "`; the trailing comma
        // and the extra spaces are real user habits, so we test
        // both. Empty tokens are dropped entirely (rather than
        // emitted as `""`) because an empty tag would always miss
        // in the registry and would silently exclude every episode.
        assert_eq!(
            parse_csv("fintech, ai , "),
            vec!["fintech".to_string(), "ai".to_string()]
        );
        assert!(parse_csv("").is_empty());
        assert!(parse_csv(",,, ,").is_empty());
    }

    #[test]
    fn truncate_keeps_char_boundary() {
        // ASCII path: every char is one byte, max=5 keeps "hello"
        // exactly and the iterator's next() returns None so no
        // ellipsis is appended.
        assert_eq!(truncate("hello", 5), "hello");
        // Truncate at char boundary, append ellipsis.
        assert_eq!(truncate("hello world", 5), "hello…");
        // Multi-byte chars must not split: an em-dash is 3 bytes
        // but 1 char. max=3 chars covers "a—b" (5 bytes) without
        // panicking the way a naive byte-slice would.
        assert_eq!(truncate("a—bcdef", 3), "a—b…");
    }
}
