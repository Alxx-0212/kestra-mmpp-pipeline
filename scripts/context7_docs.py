"""Fetch up-to-date documentation for this project's tech stack via the Context7 MCP server.

This is a small MCP client (no local MCP process required) that talks to the
hosted Context7 server over streamable HTTP:

    https://mcp.context7.com/mcp

It uses the two Context7 tools:
    * resolve-library-id  -> maps a library name to a Context7-compatible ID
    * query-docs          -> fetches version-specific docs/examples for that ID

Usage:
    python scripts/context7_docs.py
    python scripts/context7_docs.py --only kestra polars
    CONTEXT7_API_KEY=xxx python scripts/context7_docs.py   # higher rate limits

Outputs are written to docs/context7/<slug>.md plus an index.md.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

CONTEXT7_URL = "https://mcp.context7.com/mcp"
OUT_DIR = Path("docs/context7")

# (slug, libraryName to resolve, focused query for query-docs, explicit library id)
# The explicit id, when set, skips resolve-library-id (used when the resolver is
# ambiguous, e.g. "pyarrow" matching clickhouse-connect on a parquet query).
STACK = [
    ("kestra", "Kestra", "YAML workflow with io.kestra.plugin.scripts.python.Script and Docker task runner", None),
    ("python", "Python", "Python 3.11 typing, asyncio and pathlib best practices", None),
    ("polars", "Polars", "LazyFrame API, expressions and group-by aggregations", None),
    ("pandas", "pandas", "DataFrame groupby, merge and reshaping", None),
    ("pandera", "Pandera", "DataFrame/Series schema validation and checks", None),
    ("gspread", "gspread", "Authenticate with a service account and read/write worksheets", None),
    ("google-auth", "google-auth", "Build service-account credentials from a JSON key file", None),
    ("pyarrow", "pyarrow", "Python pyarrow: read_table/write_table, Table and Parquet dataset API", "/apache/arrow"),
    ("openpyxl", "openpyxl", "Write formulas, styles and multiple sheets to xlsx", None),
    ("psycopg", "psycopg", "Async connection, parameterized queries and transactions", None),
    ("postgresql", "PostgreSQL", "JSONB queries, indexing and window functions", None),
    ("docker-compose", "Docker Compose", "Multi-service networking, volumes and healthchecks", None),
    ("dbt", "dbt", "Models, materializations and sources for transformations", None),
]


def _text_from(result) -> str:
    parts = []
    for c in getattr(result, "content", []) or []:
        if getattr(c, "type", None) == "text":
            parts.append(c.text)
    return "\n".join(parts).strip()


def _first_library_id(text: str, prefer: str | None = None) -> str | None:
    candidates = re.findall(r"(/\S+/\S+)", text)
    if prefer:
        token = prefer.lower().replace(" ", "").replace("-", "")
        for cid in candidates:
            if token in cid.lower().replace("-", ""):
                return cid
    return candidates[0] if candidates else None


async def fetch_one(session: ClientSession, slug: str, name: str, query: str,
                    explicit_id: str | None) -> dict:
    resolved = ""
    library_id = explicit_id
    if not library_id:
        res = await session.call_tool(
            "resolve-library-id",
            {"libraryName": name, "query": query},
        )
        resolved = _text_from(res)
        library_id = _first_library_id(resolved, prefer=name)

    docs = ""
    if library_id:
        dres = await session.call_tool(
            "query-docs",
            {"libraryId": library_id, "query": query},
        )
        docs = _text_from(dres)

    return {
        "slug": slug,
        "name": name,
        "query": query,
        "library_id": library_id,
        "resolved": resolved,
        "docs": docs,
    }


async def run(only: list[str] | None, delay: float) -> list[dict]:
    items = [s for s in STACK if only is None or s[0] in only]
    api_key = os.environ.get("CONTEXT7_API_KEY")
    headers = {"CONTEXT7_API_KEY": api_key} if api_key else None
    http_client = httpx.AsyncClient(headers=headers) if headers else None

    results: list[dict] = []
    async with streamable_http_client(CONTEXT7_URL, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for slug, name, query, explicit_id in items:
                print(f"[context7] resolving + querying: {name}", file=sys.stderr)
                try:
                    data = await fetch_one(session, slug, name, query, explicit_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"[context7] error for {name}: {exc}", file=sys.stderr)
                    data = {
                        "slug": slug,
                        "name": name,
                        "query": query,
                        "library_id": None,
                        "resolved": "",
                        "docs": f"ERROR: {exc}",
                    }
                results.append(data)
                if delay:
                    await asyncio.sleep(delay)
    return results


def write_outputs(results: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    index_lines = ["# Context7 documentation cache", "", f"_Generated: {stamp}_", "",
                   "| Library | Context7 ID | File |", "|---|---|---|"]
    for r in results:
        lid = r["library_id"] or "(unresolved)"
        fname = f"{r['slug']}.md"
        index_lines.append(f"| {r['name']} | `{lid}` | [{fname}]({fname}) |")

        body = [
            f"# {r['name']}",
            "",
            f"- Context7 library ID: `{r['library_id'] or 'UNRESOLVED'}`",
            f"- Query: {r['query']}",
            f"- Cached (UTC): {stamp}",
            "",
            "## Documentation",
            "",
            r["docs"] or "(no documentation returned)",
            "",
        ]
        (OUT_DIR / fname).write_text("\n".join(body), encoding="utf-8")
        print(f"[context7] wrote docs/context7/{fname}", file=sys.stderr)

    (OUT_DIR / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    print(f"[context7] wrote docs/context7/index.md ({len(results)} libraries)",
          file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch project tech-stack docs via Context7 MCP")
    ap.add_argument("--only", nargs="*", help="Limit to these slugs")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Seconds to wait between libraries (avoid rate limits)")
    args = ap.parse_args()

    results = asyncio.run(run(args.only, args.delay))
    write_outputs(results)


if __name__ == "__main__":
    main()
