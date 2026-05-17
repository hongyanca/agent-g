"""Build character retrieval queries from character concern and current input."""

import re
from dataclasses import dataclass

from memory.parser import extract_status_field
from shared.narrator_output import extract_narrator_output
from storage.agent_files import read_agent_file


_QUERY_TEXT_LIMIT = 1800
_BM25_TEXT_LIMIT = 700
_UNDERSTANDING_TEXT_LIMIT = 1200


@dataclass(frozen=True)
class RetrievalQueries:
    episode: str
    episode_bm25: str
    understanding: str
    understanding_bm25: str


def get_character_concern(agent_name: str) -> str:
    """Read character's 在意的事 from their own status.md; returns empty string on failure."""
    if agent_name == "narrator":
        return ""
    status = read_agent_file(agent_name, "status.md")
    return extract_status_field(status, "在意的事").strip()


def _get_location(agent_name: str, raw_messages: list[dict] | None) -> str:
    """Extract location from the most recent visible narrator output."""
    for msg in reversed(raw_messages or []):
        if agent_name != "narrator" and agent_name not in msg.get("visible_to", []):
            continue
        payload = extract_narrator_output(msg)
        if payload:
            return str(payload.get("location") or "").strip()
    return ""


def _clip(text: str, limit: int, *, normalize: bool = True) -> str:
    if normalize:
        text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def build_retrieval_queries(
    agent_name: str,
    user_input: str,
    raw_messages: list[dict] | None = None,
) -> RetrievalQueries:
    """Build retrieval queries from character concern and current input.

    episode embedding:  在意的事 + location + user_input
      location anchors episode scenes; EpisodeMemory embedding index includes location.
    episode/understanding BM25:  在意的事 + user_input
      keyword precision; location is not a useful BM25 signal.
    understanding embedding:  在意的事 + user_input
      Understandings are not place-bound; location adds noise.
    """
    concern = get_character_concern(agent_name)
    location = _get_location(agent_name, raw_messages)

    episode_query = "\n".join(p for p in [concern, location, user_input] if p)
    shared_query = "\n".join(p for p in [concern, user_input] if p)

    bm25_query = _clip(shared_query, _BM25_TEXT_LIMIT, normalize=False) if shared_query else user_input
    return RetrievalQueries(
        episode=_clip(episode_query, _QUERY_TEXT_LIMIT) if episode_query else user_input,
        episode_bm25=bm25_query,
        understanding=_clip(shared_query, _UNDERSTANDING_TEXT_LIMIT) if shared_query else user_input,
        understanding_bm25=bm25_query,
    )
