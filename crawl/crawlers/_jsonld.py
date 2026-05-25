"""Shared JSON-LD parsing helpers for crawler metadata extraction.

Several publishers (RBC Disruptors, WEF Radio Davos, BCG, McKinsey,
…) embed canonical episode metadata inside
``<script type="application/ld+json">`` blocks. The payloads come in
three shapes — a single object, a top-level array of objects, or a
``{"@graph": [...]}`` envelope — and any combination of those can
ship per page. This module centralises the walking logic so every
crawler benefits from the same robustness when a publisher rolls
out a new shape (for example, when WEF's WordPress plugin starts
emitting an ``@graph`` envelope its old single-object parser was
silently ignoring).

The two callers today (``rbc_disruptors`` and ``wef_radio_davos``)
each had their own slightly different walker — RBC's only handled
dicts (single-object + ``@graph``), WEF's also handled top-level
arrays and ``@type``-as-list. Extracting the union keeps the parser
asymmetry from regressing into one crawler missing a shape the
other already learned to handle.

Design notes:

* **No HTML decoding.** Some publishers (RBC) leak HTML entities
  into JSON-LD ``headline`` fields via their WordPress plugin.
  Decoding is the caller's responsibility because each publisher
  may want different post-processing (strip vs. unescape vs.
  leave-as-is) and applying it here would conflate parsing with
  presentation.
* **No type filtering.** ``iter_jsonld_objects`` yields every dict
  in the payload; the caller picks the ``@type`` it cares about.
  Doing the filter here would mean every new publisher type
  (``CreativeWork``, ``PodcastEpisode``, ``Article``, …) has to
  land in this file, which is the wrong place to know that.
* **CDATA stripping is on by default.** A few publishers
  (notably WEF) wrap their JSON-LD in
  ``// <![CDATA[ … // ]]>`` because their template engine renders
  the script element inside an XML island. Stripping the shell
  is universally safe — the inner JSON never legitimately starts
  with the CDATA tokens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

# Strip the CDATA wrapper some publishers emit around JSON-LD
# payloads. The inner JSON content sits between the first ``{``
# or ``[`` and the matching last ``}`` or ``]`` — we feed the
# whole stripped string to ``json.loads`` rather than
# substring-matching with a regex, because a greedy ``{...}``
# regex would silently corrupt ``[{...}, {...}]`` array payloads.
_CDATA_SHELL_RE = re.compile(r"^\s*//\s*<!\[CDATA\[|//\s*]]>\s*$", flags=re.M)


def iter_jsonld_objects(raw: str) -> Iterator[dict[str, object]]:
    """Yield each dict in a single JSON-LD payload string.

    Handles three shapes publishers emit interchangeably:

    1. ``{"@type": "Article", ...}`` — a single object.
    2. ``[{...}, {...}]`` — a top-level array of objects.
    3. ``{"@context": ..., "@graph": [{...}, {...}]}`` — the
       ``@graph`` envelope WordPress's JSON-LD plugin produces.

    Yields nothing if the payload is empty, isn't valid JSON, or
    is a non-object/array shape (number, string, null). The
    caller can iterate without a try/except.
    """
    payload = _CDATA_SHELL_RE.sub("", raw).strip()
    if not payload:
        return
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item
            return
        yield data
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item


def type_matches(node: dict[str, object], wanted: frozenset[str]) -> bool:
    """Return True iff ``node['@type']`` matches any name in ``wanted``.

    JSON-LD permits ``@type`` to be either a single string or a
    list of strings (e.g. ``["NewsArticle", "PodcastEpisode"]``).
    A node matches when at least one of its declared types
    appears in ``wanted``.
    """
    t = node.get("@type")
    if isinstance(t, list):
        return any(tt in wanted for tt in t if isinstance(tt, str))
    return isinstance(t, str) and t in wanted
