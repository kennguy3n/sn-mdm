# sn-mdm Source Catalogue

Curated set of 17 publishers (across 2 tranches) whose freely
accessible content is admissible under the default rights gate
allowlist. Each row in the registry sits in
[`crawl/crawl_config.toml`](../crawl/crawl_config.toml) and is
backed by a concrete crawler under
[`crawl/crawlers/`](../crawl/crawlers/).

## Rights codes

| Code                       | Verbatim reuse        | Notes                                                                                   |
| -------------------------- | --------------------- | --------------------------------------------------------------------------------------- |
| `ogl_v3`                   | yes                   | UK Open Government Licence v3.0 — public-sector default.                                |
| `cc_by`                    | yes (with attribution)| Creative Commons Attribution 4.0.                                                       |
| `cc_by_sa`                 | yes (with attribution)| Creative Commons Attribution-ShareAlike 4.0.                                            |
| `cc_by_nc`                 | yes (with attribution)| Creative Commons Attribution-NonCommercial 4.0.                                         |
| `cc_by_nc_nd`              | with attribution      | Creative Commons Attribution-NonCommercial-NoDerivatives 4.0; chunks preserve heading.  |
| `free_access_copyrighted`  | with attribution      | Publisher offers the transcript free of charge under copyright; chunks cite the URL.    |
| `public_domain`            | yes                   | Public domain dedication.                                                               |
| `paywalled`                | no                    | **Rejected by default.** Never crawled.                                                 |
| `unknown`                  | no                    | **Rejected by default.** Forces the crawler to classify the rights before re-ingest.    |

## Tag families

Every episode is tagged across the 5 families
([`pack_core::metadata::TagFamilies`](../crates/pack_core/src/metadata.rs)):

| Family            | Example values                                                                                       |
| ----------------- | ---------------------------------------------------------------------------------------------------- |
| Industry          | product, marketing, HR, finance, banking, fintech, healthcare, legal, construction, retail, supply-chain, hospitality, real-estate, design-firm, startup |
| Function          | strategy, pricing, GTM, performance-marketing, hiring, culture, M&A, governance, compliance, cyber-risk, operations, procurement |
| Business model    | B2B, B2C, marketplace, SaaS, platform, regulated-incumbent, venture-backed, public-sector            |
| Geography         | US, UK, Canada, India, Singapore, Indonesia, GCC, Saudi Arabia, UAE, Switzerland, Germany, APAC, global |
| Evidence type     | full-transcript, transcript-pdf, show-notes, whitepaper, playbook, standards-guide, newsletter, webinar |

## Tranche 1 — core sources (15)

| Publisher                              | Crawler                       | Surface                                                              | Rights                    | Typical geography |
| -------------------------------------- | ----------------------------- | -------------------------------------------------------------------- | ------------------------- | ----------------- |
| Acquired                               | `acquired.py`                 | `acquired.fm/episodes/{slug}` HTML transcripts                       | `free_access_copyrighted` | US / global       |
| Andreessen Horowitz (a16z)             | `a16z.py`                     | `a16z.com/podcast/{slug}` HTML transcripts + dense summaries         | `free_access_copyrighted` | US / global       |
| BCG Featured Insights                  | `bcg.py`                      | `bcg.com/featured-insights/podcasts/{slug}` + PDF transcripts        | `free_access_copyrighted` | global            |
| McKinsey Insights                      | `mckinsey.py`                 | `mckinsey.com/{insights\|capabilities\|industries}/{...}` HTML       | `free_access_copyrighted` | global            |
| Exit Five                              | `exit_five.py`                | `exitfive.com/podcast/{slug}` HTML transcripts                       | `free_access_copyrighted` | US / global       |
| TED — WorkLife with Molly Graham       | `ted_worklife.py`             | `ted.com/podcasts/{slug}-transcript` (hub: `/podcasts/worklife-transcripts`) | `cc_by_nc_nd`             | US / global       |
| Masters of Scale                       | `masters_of_scale.py`         | `mastersofscale.com/episode/{slug}` HTML transcripts                 | `free_access_copyrighted` | US / global       |
| People Matters                         | `people_matters.py`           | `peoplematters.in/podcast/{slug}`                                    | `free_access_copyrighted` | India / SEA       |
| RBC Disruptors                         | `rbc_disruptors.py`           | `rbc.com/en/thought-leadership/disruptors/{slug}`                    | `free_access_copyrighted` | Canada            |
| IMD Business School                    | `imd.py`                      | `imd.org/ibyimd/podcasts/{slug}`                                     | `free_access_copyrighted` | Switzerland       |
| WEF Radio Davos                        | `wef_radio_davos.py`          | `weforum.org/podcasts/radio-davos/episodes/{slug}`                   | `cc_by_nc_nd`             | global            |
| Deutsche Bank — flow InCorporate Treasury | `deutsche_bank.py`         | `flow.db.com/media/flow-incorporatetreasury-podcasts/{slug}{,-transcript}` | `free_access_copyrighted` | Germany / APAC    |
| UK NCSC Toolkit for Boards             | `ncsc.py`                     | `ncsc.gov.uk/information/toolkit-for-boards-audio-transcripts`       | `ogl_v3`                  | UK                |
| Microsoft Cyber (WWPS)                 | `microsoft_cyber.py`          | `wwps.microsoft.com/{episodes,infrastructure-episodes}/{slug}` + PDF transcripts | `free_access_copyrighted` | US / global       |
| Thomson Reuters Institute              | `thomson_reuters.py`          | `thomsonreuters.com/en-us/posts/{category}/podcast-{slug}` + PDF transcripts | `free_access_copyrighted` | US / global       |

## Tranche 2 — specialist overlays (2)

| Publisher                              | Crawler                       | Surface                                                              | Rights                    | Specialist angle  |
| -------------------------------------- | ----------------------------- | -------------------------------------------------------------------- | ------------------------- | ----------------- |
| frog (Design Mind frogcast)            | `frog.py`                     | `frog.co/designmind/design-mind-frogcast-{slug}`                     | `free_access_copyrighted` | Design / strategy |
| RICS (Royal Institution of Chartered Surveyors) | `rics.py`            | `rics.org/news-insights/{slug}` + companion PDFs                     | `free_access_copyrighted` | Construction / real estate |

## Index discovery status

`BaseCrawler.initial_sync` merges the configured seed list
(`config.episodes`) with whatever
`_discover_episode_slugs` returns. A walker that returns the
empty list is purely seed-driven. The table below captures the
state on this PR — it's the canonical reference for what a
fresh `python -m crawl.pipeline` run will actually pull from
each source.

| Publisher          | Walker channel             | Status                                                                                            |
| ------------------ | -------------------------- | ------------------------------------------------------------------------------------------------- |
| `a16z`             | `podcast-sitemap.xml`      | working — 25 episode permalinks per run (capped).                                                  |
| `acquired`         | `/episodes` HTML index     | working — 20 episode slugs per run + 4 curated seeds, deduped.                                     |
| `bcg`              | `google_sitemap-content`   | working — 15 episode permalinks (Imagine This series).                                             |
| `exit_five`        | `/podcast` HTML index      | working — 12 episode slugs per run.                                                                |
| `frog`             | `/designmind` HTML index   | working — 4 frogcast permalinks.                                                                   |
| `imd`              | `/ibyimd/category/podcasts/` HTML | working — 22 episode permalinks across series; WordPress `/page/N` pagination rejected at discovery.                                              |
| `masters_of_scale` | `episode-sitemap.xml`      | working — first 25 of 700+ permalinks (WordPress sitemap).                                         |
| `microsoft_cyber`  | `post-sitemap{1,2}.xml`    | working — 25 episode permalinks (`Public Sector Future` + `Future of Infrastructure`). One per-run skip when `traffic.libsyn.com` `robots.txt` disallows a transcript PDF — expected behaviour.           |
| `ncsc`             | (single-page, seeded)      | working — 1 verbatim CISO-board transcript page, OGL-v3.                                           |
| `people_matters`   | `sitemap.xml/podcast`      | working — 25 of 61 podcast permalinks (series *hub* pages — slugs without an embedded `/<ep-slug>` — are rejected; only true `<series>/<episode>` permalinks are admitted).                                                             |
| `rbc_disruptors`   | `/en/thought-leadership/disruptors/` (headless) | working — 11 episode permalinks per run via Playwright. Discovery + per-episode fetch route through `BaseCrawler.fetch_rendered` because the archive page is a fully client-rendered React tree. |
| `wef_radio_davos`  | `/podcasts/radio-davos/` (headless)             | working — 7 episode permalinks per run via Playwright. `weforum.org` returns HTTP 403 to plain `requests` GETs (WAF on TLS/JA3 + client-hint fingerprint); the headless-Chromium transport's realistic fingerprint clears the WAF. Transcripts extracted from `<div data-gtm-section="Podcast transcript">`. |
| `deutsche_bank`    | `flow.db.com/media/flow-incorporatetreasury-podcasts/` (HTML hub) | working — 11 episode permalinks discovered, each resolved to its `{slug}-transcript` companion page for verbatim text. The original Tranche 1 base (`corporates.db.com/multimedia/podcasts`) only hosts audio-only series; flow.db.com is the only Deutsche Bank surface that publishes transcripts. Plain `requests` GET (no headless). |
| `mckinsey`         | n/a                        | **Gap — WAF**. `www.mckinsey.com/featured-insights/mckinsey-podcast` returns an Akamai "Access Denied" page even via headless Chromium with a realistic fingerprint, while sibling paths under `/featured-insights/` work. Akamai is path-scoped against the podcast subpath and our fingerprint is not enough to clear it. Will need a residential-egress proxy (or McKinsey allow-listing our origin) before discovery is possible. |
| `rics`             | n/a                        | **Gap — source-side**. `rics.org/podcast` renders fully via headless browser but the episode cards link only to external audio players (Buzzsprout) — **no transcripts are published on rics.org**. Crawling cleanly is impossible until RICS adds transcripts. |
| `ted_worklife`     | `/podcasts/worklife-transcripts` (HTML hub) | working — 4 episode-transcript permalinks discovered. The show rebranded from Adam Grant → Molly Graham and TED migrated the URL structure from `/podcasts/worklife/{slug}-transcript` (Tranche 1) to `/podcasts/{slug}-transcript` (now). The hub `/podcasts/worklife-transcripts` is the canonical listing under the new scheme. Plain `requests` GET (no headless). |
| `thomson_reuters`  | `post-sitemap{1,2,3}.xml` (sitemap) | working — 25 of the 47 `podcast-*` permalinks discovered (DISCOVER_CAP). Each post embeds an "Episode transcript" PDF link under `wp-content/uploads/`; the crawler fetches the PDF for verbatim text and falls back to the post HTML when the PDF 404s (stale links on a handful of older posts). Plain `requests` GET (no headless). |

Two `Gap` rows remain after Milestone A2 — both are blocked on
factors outside the codebase:

* **Source-side gap** (`rics`): RICS publishes audio only on
  `rics.org/podcast`; episode cards link out to Buzzsprout for
  playback and no transcripts are hosted on the RICS site. No
  technical change to the crawler will close this — it's blocked
  on the publisher.
* **WAF gap** (`mckinsey`): Akamai still 403s the specific
  podcast subpath even with a realistic browser fingerprint.
  Closing this requires a residential-egress proxy or an explicit
  origin allow-listing by McKinsey.

The architecture supports both shapes — `_discover_episode_slugs`
is there, the headless-browser transport is wired in (see
`crawl/crawlers/_browser.py`), and the rest of the pipeline is
publisher-agnostic. Closing the remaining two gaps requires
changes on the source side (or a different network egress for
McKinsey), not in this codebase.

## Adding a new source

1. Pick a stable `publisher_id` (lower-snake-case).
2. Create `crawl/crawlers/{publisher_id}.py` with a class deriving
   `BaseCrawler` and implementing `_episode_url`,
   `fetch_transcript`, a source-specific `_normalize_html_bytes`
   (or `_normalize_pdf_bytes`), and — unless the source is purely
   seed-driven — `_discover_episode_slugs` returning up to
   `DISCOVER_CAP` (default 25) per-episode slugs.
3. Register the class in `crawl/crawlers/__init__.py::_REGISTRY`.
4. Add a `[sources.{publisher_id}]` block to `crawl_config.toml`
   with the tag families and rights code.
5. Add a row to this catalogue.
6. Run the registry test:
   ```bash
   pytest crawl/tests/test_config.py -k registry_covers_all_known_publishers
   ```

The pipeline rejects unregistered publisher ids at TOML load
time, so any drift between the registry and the catalogue is
caught at boot.
