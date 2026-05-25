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
| TED — WorkLife with Adam Grant         | `ted_worklife.py`             | `ted.com/podcasts/worklife/{slug}-transcript`                        | `cc_by_nc_nd`             | US / global       |
| Masters of Scale                       | `masters_of_scale.py`         | `mastersofscale.com/episode/{slug}` HTML transcripts                 | `free_access_copyrighted` | US / global       |
| People Matters                         | `people_matters.py`           | `peoplematters.in/podcast/{slug}`                                    | `free_access_copyrighted` | India / SEA       |
| RBC Disruptors                         | `rbc_disruptors.py`           | `rbc.com/en/thought-leadership/disruptors/{slug}`                    | `free_access_copyrighted` | Canada            |
| IMD Business School                    | `imd.py`                      | `imd.org/ibyimd/category/podcasts/{slug}`                            | `free_access_copyrighted` | Switzerland       |
| WEF Radio Davos                        | `wef_radio_davos.py`          | `weforum.org/podcasts/radio-davos/episodes/{slug}`                   | `cc_by_nc_nd`             | global            |
| Deutsche Bank                          | `deutsche_bank.py`            | `corporates.db.com/multimedia/{slug}` + `db.com/news/detail/{slug}`  | `free_access_copyrighted` | Germany / APAC    |
| UK NCSC Toolkit for Boards             | `ncsc.py`                     | `ncsc.gov.uk/information/toolkit-for-boards-audio-transcripts`       | `ogl_v3`                  | UK                |
| Microsoft Cyber (WWPS)                 | `microsoft_cyber.py`          | `wwps.microsoft.com/blog/episodes/{slug}` + PDF transcripts          | `free_access_copyrighted` | US / global       |
| Thomson Reuters Legal                  | `thomson_reuters.py`          | `thomsonreuters.com/en-us/posts/legal/{slug}` HTML transcripts       | `free_access_copyrighted` | US / global       |

## Tranche 2 — specialist overlays (2)

| Publisher                              | Crawler                       | Surface                                                              | Rights                    | Specialist angle  |
| -------------------------------------- | ----------------------------- | -------------------------------------------------------------------- | ------------------------- | ----------------- |
| frog (Design Mind frogcast)            | `frog.py`                     | `frog.co/designmind/design-mind-frogcast-{slug}`                     | `free_access_copyrighted` | Design / strategy |
| RICS (Royal Institution of Chartered Surveyors) | `rics.py`            | `rics.org/news-insights/{slug}` + companion PDFs                     | `free_access_copyrighted` | Construction / real estate |

## Adding a new source

1. Pick a stable `publisher_id` (lower-snake-case).
2. Create `crawl/crawlers/{publisher_id}.py` with a class deriving
   `BaseCrawler` and implementing `_episode_url`,
   `fetch_transcript`, and a source-specific `_normalize_html_bytes`
   (or `_normalize_pdf_bytes`).
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
