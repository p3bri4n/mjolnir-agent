"""
Loads the "skills" present in /app/skills (a convention similar to
skills/<name>/SKILL.md with a short description at the top of the file)
and offers a very simple keyword match. Replace with a real reranker
(see Context Manager) if the number of skills grows.
"""

import re
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Skill Manager")

SKILLS_DIR = Path("/app/skills")


def load_skills():
    skills = {}
    if not SKILLS_DIR.exists():
        return skills
    for skill_file in SKILLS_DIR.glob("*/SKILL.md"):
        name = skill_file.parent.name
        content = skill_file.read_text(encoding="utf-8")
        # convention: the description is the 1st non-empty line after any title
        description_match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE | re.IGNORECASE)
        description = description_match.group(1).strip() if description_match else content.split("\n", 1)[0]
        skills[name] = {"name": name, "description": description, "content": content}
    return skills


class MatchRequest(BaseModel):
    query: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/skills")
async def list_skills():
    return {"skills": list(load_skills().values())}


@app.get("/skills/{name}")
async def get_skill(name: str):
    skills = load_skills()
    return skills.get(name, {})


@app.post("/match")
async def match_skill(request: MatchRequest):
    """Returns the skill whose description shares the most words with the query."""
    query_words = set(re.findall(r"\w+", request.query.lower()))
    best_skill, best_score = None, 0

    for skill in load_skills().values():
        desc_words = set(re.findall(r"\w+", skill["description"].lower()))
        score = len(query_words & desc_words)
        if score > best_score:
            best_skill, best_score = skill, score

    return {"skill": best_skill if best_score > 0 else None}
