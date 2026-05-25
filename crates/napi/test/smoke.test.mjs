// Smoke tests for the Node.js native addon produced by `napi build`
// in `crates/napi/`. Loads the platform-specific `.node` artefact
// via the generated `index.js` loader, exercises every exported
// function at least once, and asserts the JSON-envelope error
// contract documented in `crates/napi/src/bindings.rs`.
//
// Runs under `node --test test/`. Exits non-zero on any assertion
// failure so CI can fail fast.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, resolve as resolvePath, join as joinPath } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const requireCJS = createRequire(import.meta.url);
const core = requireCJS(resolvePath(here, '..', 'index.js'));

// ----------------------------------------------------------------
// Fixture helpers
// ----------------------------------------------------------------

// Build a real ``.pack`` file on disk by shelling out to the
// ``pack-build`` Rust binary over a small synthetic JSONL fixture.
// Using a real pack (rather than a hand-rolled byte buffer) is the
// only way to exercise the full open + verify + extract + search
// path the napi bridge takes in production.
function buildTestPack() {
  const workdir = mkdtempSync(joinPath(tmpdir(), 'pack-napi-smoke-'));
  const packsRoot = joinPath(workdir, 'packs');
  for (const sub of ['metadata', 'chunks', 'governance']) {
    execFileSync('mkdir', ['-p', joinPath(packsRoot, sub)]);
  }

  const episode = {
    episode_id: 'smoketest_flagship_widgets',
    publisher: 'smoketest',
    series: 'flagship',
    title: 'Widgets',
    host: ['Host One'],
    guest: [],
    publication_date: '2025-01-01',
    country_region: ['US'],
    industry_tags: ['manufacturing'],
    function_tags: ['operations'],
    business_model_tags: ['B2B'],
    source_type: 'podcast_transcript_html',
    language: 'en',
    primary_url: 'https://example.com/ep/1',
    asset_urls: [],
    rights_code: 'free_access_copyrighted',
    rights_summary: 'Test fixture only.',
    credibility_notes: '',
    summary: 'A test episode about widget manufacturing.',
    chunking_policy: { target_tokens: 700, overlap_tokens: 120 },
  };
  const chunk = {
    chunk_id: 'smoketest_flagship_widgets#0001',
    episode_id: episode.episode_id,
    token_count: 16,
    section_heading: 'Manufacturing',
    chunk_text:
      'HOST: Widgets are manufactured in batches of one hundred. The factory runs three shifts.',
    citation_anchor: `${episode.primary_url}#section-1`,
  };
  const governance = {
    episode_id: episode.episode_id,
    rights_code: episode.rights_code,
    ingestion_date: 1735_689_600, // 2025-01-01T00:00:00Z
    content_hash: 'a'.repeat(64), // 32 zero bytes hex-encoded
    deprecated: false,
  };
  writeFileSync(
    joinPath(packsRoot, 'metadata', 'smoketest.jsonl'),
    JSON.stringify(episode) + '\n',
  );
  writeFileSync(
    joinPath(packsRoot, 'chunks', 'smoketest.jsonl'),
    JSON.stringify(chunk) + '\n',
  );
  writeFileSync(
    joinPath(packsRoot, 'governance', 'rights_log.jsonl'),
    JSON.stringify(governance) + '\n',
  );

  const packPath = joinPath(workdir, 'smoke.pack');
  // Resolve the workspace root by walking up from this test file
  // (`crates/napi/test/`).
  const workspaceRoot = resolvePath(here, '..', '..', '..');
  execFileSync(
    'cargo',
    [
      'run',
      '--manifest-path',
      joinPath(workspaceRoot, 'Cargo.toml'),
      '--quiet',
      '--bin',
      'pack-build',
      '--',
      '--packs-root',
      packsRoot,
      '--output',
      packPath,
    ],
    { stdio: 'inherit' },
  );

  return { workdir, packPath };
}

function cleanup(workdir) {
  try {
    rmSync(workdir, { recursive: true, force: true });
  } catch (_err) {
    // Best-effort cleanup; tempdirs are eventually reaped by the OS.
  }
}

// ----------------------------------------------------------------
// Surface assertions
// ----------------------------------------------------------------

test('exports the documented surface', () => {
  assert.equal(typeof core.openPack, 'function');
  assert.equal(typeof core.search, 'function');
  assert.equal(typeof core.closePack, 'function');
});

// ----------------------------------------------------------------
// Error envelope contract
// ----------------------------------------------------------------

test('openPack on missing file raises Io envelope', () => {
  try {
    core.openPack('/nonexistent/path/to/missing.pack');
    assert.fail('expected openPack to throw on missing file');
  } catch (err) {
    const parsed = JSON.parse(err.message);
    assert.equal(parsed.kind, 'Io');
    assert.ok(parsed.message.length > 0);
  }
});

test('openPack on truncated file raises TruncatedPack envelope', () => {
  const workdir = mkdtempSync(joinPath(tmpdir(), 'pack-napi-truncated-'));
  const path = joinPath(workdir, 'truncated.pack');
  // 4 bytes — strictly less than MIN_HEADER_BYTES (22). PackReader
  // returns TruncatedPack.
  writeFileSync(path, Buffer.from('SNMD'));
  try {
    core.openPack(path);
    assert.fail('expected openPack to throw on truncated file');
  } catch (err) {
    const parsed = JSON.parse(err.message);
    assert.equal(parsed.kind, 'TruncatedPack');
  } finally {
    cleanup(workdir);
  }
});

test('search on zero (sentinel) handle raises InvalidArgument', () => {
  try {
    core.search(0n, {});
    assert.fail('expected search(0n) to throw');
  } catch (err) {
    const parsed = JSON.parse(err.message);
    assert.equal(parsed.kind, 'InvalidArgument');
  }
});

test('closePack on unknown handle returns false (not throws)', () => {
  // ``999_999n`` was never minted in this process. Idempotent
  // close must return false, not raise.
  const removed = core.closePack(999_999n);
  assert.equal(removed, false);
});

test('closePack on zero (sentinel) handle returns false', () => {
  const removed = core.closePack(0n);
  assert.equal(removed, false);
});

// ----------------------------------------------------------------
// Round-trip: open a real pack, run a search, close
// ----------------------------------------------------------------

test('open → search → close round-trips against a real .pack', () => {
  const { workdir, packPath } = buildTestPack();
  try {
    const handle = core.openPack(packPath);
    assert.equal(typeof handle, 'bigint');
    assert.notEqual(handle, 0n);

    const hits = core.search(handle, { text: 'widgets', limit: 5 });
    assert.ok(Array.isArray(hits));
    assert.equal(hits.length, 1);
    const hit = hits[0];
    assert.equal(hit.chunk_id, 'smoketest_flagship_widgets#0001');
    assert.equal(hit.episode_id, 'smoketest_flagship_widgets');
    assert.ok(typeof hit.rank_score === 'number');
    // BM25 should fire because the text query matches.
    assert.ok(hit.bm25_score !== null && hit.bm25_score !== undefined);

    const closed = core.closePack(handle);
    assert.equal(closed, true);

    // After close, a follow-up search must surface
    // InvalidArgument (the handle is no longer in the registry).
    try {
      core.search(handle, { text: 'widgets' });
      assert.fail('expected post-close search to throw');
    } catch (err) {
      const parsed = JSON.parse(err.message);
      assert.equal(parsed.kind, 'InvalidArgument');
    }

    // Idempotent double-close returns false.
    assert.equal(core.closePack(handle), false);
  } finally {
    cleanup(workdir);
  }
});

test('tag-only search returns the chunk via structured match', () => {
  const { workdir, packPath } = buildTestPack();
  try {
    const handle = core.openPack(packPath);
    const hits = core.search(handle, {
      text: '',
      tags: { industry: ['manufacturing'] },
      limit: 5,
    });
    assert.equal(hits.length, 1);
    assert.equal(hits[0].chunk_id, 'smoketest_flagship_widgets#0001');
    // Tag-only queries always set ``tag_match = true``.
    assert.equal(hits[0].tag_match, true);
    core.closePack(handle);
  } finally {
    cleanup(workdir);
  }
});

test('search returns empty array when nothing matches', () => {
  const { workdir, packPath } = buildTestPack();
  try {
    const handle = core.openPack(packPath);
    const hits = core.search(handle, { text: 'no_such_token_anywhere' });
    assert.deepEqual(hits, []);
    core.closePack(handle);
  } finally {
    cleanup(workdir);
  }
});

test('multiple open packs have distinct handles', () => {
  const a = buildTestPack();
  const b = buildTestPack();
  try {
    const ha = core.openPack(a.packPath);
    const hb = core.openPack(b.packPath);
    assert.notEqual(ha, hb);
    core.closePack(ha);
    core.closePack(hb);
  } finally {
    cleanup(a.workdir);
    cleanup(b.workdir);
  }
});

test('malformed search request raises InvalidArgument', () => {
  const { workdir, packPath } = buildTestPack();
  try {
    const handle = core.openPack(packPath);
    try {
      // ``tags`` as a string instead of an object: cannot
      // deserialise into the typed QueryRequest.
      core.search(handle, { tags: 'industry' });
      assert.fail('expected malformed request to throw');
    } catch (err) {
      const parsed = JSON.parse(err.message);
      assert.equal(parsed.kind, 'InvalidArgument');
    } finally {
      core.closePack(handle);
    }
  } finally {
    cleanup(workdir);
  }
});
