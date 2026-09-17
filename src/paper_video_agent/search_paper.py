from typing import Any, Literal

import httpx
import xmltodict
import hashlib

from sqlalchemy import select

from push_agent.basedata.database import SessionLocal
from push_agent.basedata.models import PaperEntry

ARXIV_API_URL = "https://export.arxiv.org/api/query"

ATOM_API_URL = "https://rss.arxiv.org/atom"

ArxivCategory = Literal[
    "cs.AI",
    "cs.CL",
    "cs.LG",
    "cs.MA",
    "cs.SE",
    "cs.IR",
    "cs.DB",
    "cs.DC",
    "cs.OS",
    "cs.PL",
    "cs.PF",
    "cs.CR",
    "cs.CV",
]

def search_paper(arxiv_url: str) -> list[dict]:

    try:
        print(arxiv_url)

        response = httpx.get(
            arxiv_url,
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": "push-agent/0.1",
            },
        )

        response.raise_for_status()

        search_result = xmltodict.parse(response.text)

        feed = search_result.get("feed", {})
        entries = feed.get("entry", []) or []
        if isinstance(entries, dict):
            entries = [entries]
        return entries

    except Exception as e:
        print(f"访问arxiv失败：{e}")
        return []

def sha256_string(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()

def get_existing_sha256(
    sha256_set: set[str],
) -> set[str]:

    if not sha256_set:
        return set()

    with SessionLocal() as session:
        stmt = select(
            PaperEntry.paper_sha256
        ).where(
            PaperEntry.paper_sha256.in_(sha256_set)
        )

        return set(
            session.scalars(stmt).all()
        )


def atom_api(search_query: ArxivCategory = "cs.AI"):
    entries = search_paper(f"{ATOM_API_URL}/{search_query}") or []


def filter_data(entries: list[dict]) -> list[dict]:
    if not entries:
        return []





def filter_paper(
    search_query: ArxivCategory = "cs.AI",
    start: int = 0,
    max_results: int = 200,
    sort_by: str = "submittedDate",
    sort_order: str = "descending",
) -> dict[str, Any]:

    entries = search_paper(f"{ATOM_API_URL}/{search_query}") or []

    if not entries:
        entries = search_paper(
            f"{ARXIV_API_URL}?search_query=cat:{search_query}&start={start}&max_results={max_results}&sortBy={sort_by}&sortOrder={sort_order}"
        ) or []

    if not entries:
        return {"entries": [], "count": 0}

    sha256_set = set()
    entries_filtered = []
    for entry in entries:
        arxiv_id = entry.get("id")
        if not arxiv_id:
            continue
        paper_sha256 = sha256_string(arxiv_id)
        if paper_sha256 in sha256_set:
            continue
        sha256_set.add(paper_sha256)
        entry["paper_sha256"] = paper_sha256

        links = entry.get("link", []) or []
        if isinstance(links, dict):
            links = [links]
        for link in links:
            _type = link.get("@type")
            _href = link.get("@href")
            if _type == 'text/html':
                entry["abs_url"] = _href
                pdf_href = _href.replace("abs", "pdf")
                entry["pdf_url"] = pdf_href
            if _type == 'application/pdf':
                entry["pdf_url"] = _href

        entries_filtered.append(entry)
    sha256_db_set = get_existing_sha256(sha256_set)
    entries_filtered_db = [
        entry for entry in entries_filtered if entry["paper_sha256"] not in sha256_db_set
    ]
    return {
        "entries": entries_filtered_db,
        "count": len(entries_filtered_db),
    }

if __name__ == "__main__":
    data = filter_paper(
        search_query="cs.AI",
        max_results=200,
    )
    print(data)