"""
OSRS Quest Scraper
==================
Scrapes quest requirements and rewards from the OSRS Wiki using the MediaWiki API.
Uses mwparserfromhell to parse wikitext templates cleanly without brittle HTML scraping.

Output: quests.json  — a list of quest objects conforming to the schema below.

Quest schema:
{
    "id":                   str,   # URL-safe quest name (e.g. "Cook's_Assistant")
    "name":                 str,   # Display name
    "series":               str | null,
    "members":              bool,
    "quest_points":         int,

    "requirements": {
        "quests":           [str], # Quest names that must be completed first
        "skills": [
            {"skill": str, "level": int, "boostable": bool}
        ],
        "items": [
            {"item": str, "quantity": int, "obtainable_in_quest": bool}
        ],
        "other":            [str], # Free-text requirements (e.g. "Able to defeat a level 172 enemy")
    },

    "rewards": {
        "quest_points":     int,
        "xp": [
            {"skill": str, "amount": int}
        ],
        "items": [
            {"item": str, "quantity": int}
        ],
        "unlocks":          [str], # Free-text unlock descriptions
    }
}

Dependencies:
    pip install requests mwparserfromhell

Rate limiting:
    The OSRS Wiki asks bots to set a descriptive User-Agent and avoid hammering
    the API. This scraper uses a 0.5 s delay between page requests and sends a
    proper User-Agent. See: https://oldschool.runescape.wiki/w/RuneScape:Bots
"""

import json
import re
import time
from typing import Any

import mwparserfromhell
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = "https://oldschool.runescape.wiki/api.php"
USER_AGENT = (
    "osrs-completion-optimizer/0.1 "
    "(personal research project; contact: dumboCreates@gmail.com)"
)
REQUEST_DELAY = 0.5  # seconds between API calls — be polite to the wiki

SKILLS = {
    "attack", "strength", "defence", "ranged", "prayer", "magic",
    "runecraft", "construction", "hitpoints", "agility", "herblore",
    "thieving", "crafting", "fletching", "slayer", "hunter",
    "mining", "smithing", "fishing", "cooking", "firemaking",
    "woodcutting", "farming", "sailing"
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def api_get(params: dict) -> dict:
    """Make a GET request to the MediaWiki API and return the JSON response."""
    params.setdefault("format", "json")
    response = session.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return response.json()


# ---------------------------------------------------------------------------
# Step 1: Fetch the full list of quest page titles
# ---------------------------------------------------------------------------

def get_quest_titles() -> list[str]:
    """
    Pull every page in the 'Quests' category from the MediaWiki API.
    We use the 'categorymembers' query, paginating with 'cmcontinue'.
    Returns a sorted list of page titles.
    """
    titles = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Quests",
        "cmtype": "page",
        "cmlimit": "500",
    }

    while True:
        data = api_get(params)
        members = data.get("query", {}).get("categorymembers", [])
        for m in members:
            title = m["title"]
            # Filter out meta-pages like "Quests/List", redirect stubs, etc.
            if "/" not in title and ":" not in title:
                titles.append(title)

        if "continue" not in data:
            break
        params["cmcontinue"] = data["continue"]["cmcontinue"]

    return sorted(set(titles))


# ---------------------------------------------------------------------------
# Step 2: Fetch raw wikitext for a single page
# ---------------------------------------------------------------------------

def get_wikitext(title: str) -> str | None:
    """Fetch the raw wikitext for a wiki page title. Returns None on failure."""
    params = {
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
    }
    data = api_get(params)
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return None
        revisions = page.get("revisions", [])
        if revisions:
            return revisions[0]["slots"]["main"]["*"]
    return None


# ---------------------------------------------------------------------------
# Step 3: Parse quest requirements from wikitext
# ---------------------------------------------------------------------------

def _strip_wikitext(text: str) -> str:
    """Remove wiki markup from a string, returning plain text."""
    # Remove [[link|display]] → display, or [[link]] → link
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    # Remove {{template}} calls (non-nested, simple)
    text = re.sub(r"\{\{[^}]+\}\}", "", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Normalise whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_level_from_text(text: str) -> int | None:
    """Extract the first integer that looks like a skill level from text."""
    m = re.search(r"\b(\d{1,2})\b", text)
    return int(m.group(1)) if m else None


def _is_boostable(text: str) -> bool:
    """Detect common wiki conventions for boostable skill requirements."""
    lower = text.lower()
    return any(kw in lower for kw in ("boost", "boostable", "assistable", "can be boosted"))


def _get_quest_details_param(wikicode: mwparserfromhell.wikicode.Wikicode, param_name: str) -> str | None:
    """Return the string value of a named parameter from {{Quest details}}, or None."""
    for template in wikicode.filter_templates():
        if "quest details" in template.name.strip().lower():
            try:
                return str(template.get(param_name).value)
            except ValueError:
                return None
    return None


def parse_skill_requirements(wikicode: mwparserfromhell.wikicode.Wikicode) -> list[dict]:
    """
    Extract skill requirements from the wikitext.

    The OSRS wiki stores skill requirements inside the |requirements parameter
    of {{Quest details}} using {{SCP|SkillName|Level|link=yes}} templates.
    Boostability is indicated by an adjacent {{Boostable|yes/no}} template on
    the same wikitext line.
    """
    skill_reqs = []
    seen = set()

    # Only scan the |requirements param of {{Quest details}} so we don't pick
    # up recommended/suggested skills from |recommended or other params.
    req_text = _get_quest_details_param(wikicode, "requirements")
    if not req_text:
        return skill_reqs

    req_wikicode = mwparserfromhell.parse(req_text)
    wikitext = req_text  # used for boostable line-context lookup

    for template in req_wikicode.filter_templates():
        name = template.name.strip().lower()
        # SCP = the wiki's "Skill ClickPic" shorthand; also handle legacy names
        if name in ("scp", "skill clickpic", "skill", "skill req", "skillreq"):
            try:
                params = [str(p.value).strip() for p in template.params]
                if len(params) >= 2:
                    skill_candidate = params[0].lower()
                    level_candidate = params[1]
                    if skill_candidate not in SKILLS:
                        skill_candidate, level_candidate = level_candidate, skill_candidate
                        skill_candidate = skill_candidate.lower()
                    if skill_candidate in SKILLS:
                        try:
                            level = int(re.sub(r"\D", "", level_candidate))
                            # {{Boostable|yes}} appears on the same line in the raw wikitext
                            tmpl_str = str(template)
                            line_start = wikitext.rfind("\n", 0, wikitext.find(tmpl_str)) + 1
                            line_end = wikitext.find("\n", wikitext.find(tmpl_str))
                            line_ctx = wikitext[line_start: line_end if line_end != -1 else None]
                            boostable = bool(re.search(r"\{\{Boostable\|yes", line_ctx, re.IGNORECASE))
                            key = (skill_candidate, level)
                            if key not in seen:
                                seen.add(key)
                                skill_reqs.append({
                                    "skill": skill_candidate,
                                    "level": level,
                                    "boostable": boostable,
                                })
                        except ValueError:
                            pass
            except Exception:
                pass

    return skill_reqs


def parse_quest_requirements(wikicode: mwparserfromhell.wikicode.Wikicode) -> list[str]:
    """
    Extract prerequisite quest names from the |requirements param of
    {{Quest details}}.

    Quest prerequisite lines look like:  **[[Quest Name]]
    Non-quest lines have other text on the same line, e.g.:
      * 32 [[Quest points]] to start
      * Ability to defeat [[Elvarg]] (level 83)
    We only include links where the rest of the line (after stripping the link
    and bullet chars) contains no substantive words (4+ letters).
    """
    prereqs = []
    seen = set()

    req_text = _get_quest_details_param(wikicode, "requirements")
    if not req_text:
        return prereqs

    for line in req_text.splitlines():
        bare = line.strip("* \t")
        links = re.findall(r"\[\[([^\]]+)\]\]", bare)
        if not links:
            continue
        # Strip all wikilinks from the line; what's left should be empty for
        # pure quest-name lines. Any 4+ letter word remaining means the link
        # is incidental (skill level, boss name, etc.) rather than a prereq.
        remainder = re.sub(r"\[\[[^\]]+\]\]", "", bare).strip()
        if re.search(r"[a-zA-Z]{4,}", remainder):
            continue

        for raw in links:
            target = raw.split("|")[0].strip()
            if (
                ":" not in target
                and target.lower() not in SKILLS
                and target not in seen
                and len(target) > 3
            ):
                seen.add(target)
                prereqs.append(target)

    return prereqs


def parse_item_requirements(wikicode: mwparserfromhell.wikicode.Wikicode) -> list[dict]:
    """
    Extract item requirements from the |items param of {{Quest details}}.
    """
    items = []
    seen = set()

    section = _get_quest_details_param(wikicode, "items")
    if not section:
        return items

    for line in section.splitlines():
        clean = _strip_wikitext(line).strip("* ").strip()
        if not clean:
            continue

        # Parse optional quantity prefix: "3 logs" or "logs (3)" or just "logs"
        qty_match = re.match(r"^(\d+)\s+(.+)$", clean)
        if qty_match:
            quantity = int(qty_match.group(1))
            item_name = qty_match.group(2).strip()
        else:
            qty_match2 = re.search(r"\((\d+)\)", clean)
            quantity = int(qty_match2.group(1)) if qty_match2 else 1
            item_name = re.sub(r"\(\d+\)", "", clean).strip()

        if item_name and item_name not in seen:
            seen.add(item_name)
            # Detect items noted as obtainable during the quest
            obtainable = any(
                kw in line.lower()
                for kw in ("can be obtained", "during quest", "obtained during")
            )
            items.append({
                "item": item_name,
                "quantity": quantity,
                "obtainable_in_quest": obtainable,
            })

    return items


def parse_other_requirements(wikicode: mwparserfromhell.wikicode.Wikicode) -> list[str]:
    """Capture free-text requirements (combat, access, etc.) from |requirements param."""
    other = []

    section = _get_quest_details_param(wikicode, "requirements")
    if not section:
        return other

    for line in section.splitlines():
        clean = _strip_wikitext(line).strip("* ").strip()
        if not clean:
            continue
        # Include lines that aren't pure skill requirements and look
        # like narrative requirements (combat level, ability to defeat, etc.)
        lower = clean.lower()
        if any(kw in lower for kw in (
            "able to", "defeat", "complete", "combat level",
            "access to", "started", "started the", "partial completion"
        )):
            other.append(clean)

    return other


# ---------------------------------------------------------------------------
# Step 4: Parse quest rewards from wikitext
# ---------------------------------------------------------------------------

def parse_rewards(wikicode: mwparserfromhell.wikicode.Wikicode) -> dict:
    """
    Parse quest rewards from the {{Quest rewards}} template.
    Falls back to a raw ==Rewards== section scan if the template is absent.
    Returns a dict with keys: quest_points, xp, items, unlocks.
    """
    rewards: dict[str, Any] = {
        "quest_points": 0,
        "xp": [],
        "items": [],
        "unlocks": [],
    }

    # --- Primary path: {{Quest rewards}} template ----------------------------
    # QP lives in |qp, reward bullet-points live in |rewards.
    section: str | None = None
    for template in wikicode.filter_templates():
        if "quest rewards" in template.name.strip().lower():
            if template.has("qp"):
                try:
                    rewards["quest_points"] = int(
                        re.sub(r"\D", "", str(template.get("qp").value).strip())
                    )
                except ValueError:
                    pass
            if template.has("rewards"):
                section = str(template.get("rewards").value)
            break

    # --- Fallback: raw ==Rewards== section (some legacy pages) ---------------
    if section is None:
        text = str(wikicode)
        m = re.search(r"==\s*[Rr]ewards?\s*==(.+?)(?:==|\Z)", text, re.DOTALL)
        if m:
            section = m.group(1)
            # Also try to pull QP from plain text in this section
            qp_match = re.search(r"(\d+)\s*[Qq]uest\s*[Pp]oint", section)
            if qp_match:
                rewards["quest_points"] = int(qp_match.group(1))

    if section is None:
        return rewards

    # --- XP rewards ----------------------------------------------------------
    xp_seen: set[tuple[str, int]] = set()

    # Primary form: {{SCP|Cooking|300|link=yes}} [[experience]]
    # The wiki stores XP rewards as SCP templates followed by "experience" on the same line.
    rewards_wc = mwparserfromhell.parse(section)
    for template in rewards_wc.filter_templates():
        if template.name.strip().lower() == "scp":
            try:
                params = [str(p.value).strip() for p in template.params]
                if len(params) >= 2:
                    skill_candidate = params[0].lower()
                    amount_candidate = params[1]
                    if skill_candidate in SKILLS:
                        tmpl_str = str(template)
                        pos = section.find(tmpl_str)
                        if pos != -1:
                            line_end = section.find("\n", pos)
                            line_ctx = section[pos: line_end if line_end != -1 else None]
                            if "experience" in line_ctx.lower():
                                amount = int(re.sub(r"\D", "", amount_candidate))
                                key = (skill_candidate, amount)
                                if key not in xp_seen:
                                    xp_seen.add(key)
                                    rewards["xp"].append({"skill": skill_candidate, "amount": amount})
            except (ValueError, Exception):
                pass

    # Text fallback: "12,000 Cooking experience"
    for match in re.finditer(r"([\d,]+)\s+([A-Z][a-z]+)\s+[Ee]xperience", section):
        amount_str = match.group(1).replace(",", "")
        skill = match.group(2).lower()
        if skill in SKILLS and amount_str.isdigit():
            key = (skill, int(amount_str))
            if key not in xp_seen:
                xp_seen.add(key)
                rewards["xp"].append({"skill": skill, "amount": int(amount_str)})

    # Reverse text fallback: "Cooking experience (12,000)"
    for match in re.finditer(r"([A-Z][a-z]+)\s+[Ee]xperience\s*\(([\d,]+)\)", section):
        skill = match.group(1).lower()
        amount_str = match.group(2).replace(",", "")
        if skill in SKILLS and amount_str.isdigit():
            key = (skill, int(amount_str))
            if key not in xp_seen:
                xp_seen.add(key)
                rewards["xp"].append({"skill": skill, "amount": int(amount_str)})

    # --- Items and unlocks ---------------------------------------------------
    item_seen: set[str] = set()
    for line in section.splitlines():
        clean = _strip_wikitext(line).strip("* ").strip()
        if not clean:
            continue
        if re.search(r"[Qq]uest\s*[Pp]oint|[Ee]xperience|[Xx][Pp]", clean):
            continue

        qty_match = re.match(r"^(\d+)\s+x?\s*(.+)$", clean)
        if qty_match:
            qty = int(qty_match.group(1))
            item = qty_match.group(2).strip()
        else:
            qty = 1
            item = clean

        if any(kw in item.lower() for kw in (
            "access", "ability", "unlock", "teleport", "shortcut",
            "use of", "can now", "allowed to"
        )):
            if item not in rewards["unlocks"]:
                rewards["unlocks"].append(item)
        elif len(item) > 2 and item not in item_seen:
            item_seen.add(item)
            rewards["items"].append({"item": item, "quantity": qty})

    return rewards


# ---------------------------------------------------------------------------
# Step 5: Parse the Infobox Quest template for top-level metadata
# ---------------------------------------------------------------------------

def parse_infobox(wikicode: mwparserfromhell.wikicode.Wikicode) -> dict:
    """Extract top-level quest metadata from the {{Infobox Quest}} template."""
    meta: dict[str, Any] = {
        "series": None,
        "members": False,
        "quest_points_infobox": 0,
    }

    for template in wikicode.filter_templates():
        if "infobox quest" in template.name.strip().lower():
            for param in template.params:
                key = param.name.strip().lower()
                val = str(param.value).strip()
                if key == "series" and val:
                    meta["series"] = _strip_wikitext(val) or None
                elif key == "members":
                    meta["members"] = val.lower() in ("yes", "true", "1")
                elif key == "qp":
                    try:
                        meta["quest_points_infobox"] = int(
                            re.sub(r"\D", "", val)
                        )
                    except ValueError:
                        pass
            break

    return meta


# ---------------------------------------------------------------------------
# Step 6: Assemble a single quest record
# ---------------------------------------------------------------------------

def scrape_quest(title: str) -> dict | None:
    """
    Fetch and parse a single quest page. Returns a structured dict or None
    if the page cannot be parsed as a quest.
    """
    wikitext = get_wikitext(title)
    if wikitext is None:
        print(f"  [SKIP] '{title}' — page not found")
        return None

    wikicode = mwparserfromhell.parse(wikitext)

    # Confirm this is actually a quest page
    has_infobox = any(
        "infobox quest" in t.name.strip().lower()
        for t in wikicode.filter_templates()
    )
    if not has_infobox:
        print(f"  [SKIP] '{title}' — no Infobox Quest found")
        return None

    infobox = parse_infobox(wikicode)
    skill_reqs = parse_skill_requirements(wikicode)
    quest_prereqs = parse_quest_requirements(wikicode)
    item_reqs = parse_item_requirements(wikicode)
    other_reqs = parse_other_requirements(wikicode)
    rewards = parse_rewards(wikicode)

    # Prefer the QP value from the rewards section if available; fall back to infobox
    qp = rewards["quest_points"] or infobox["quest_points_infobox"]

    return {
        "id": title.replace(" ", "_"),
        "name": title,
        "series": infobox["series"],
        "members": infobox["members"],
        "quest_points": qp,
        "requirements": {
            "quests": quest_prereqs,
            "skills": skill_reqs,
            "items": item_reqs,
            "other": other_reqs,
        },
        "rewards": rewards,
    }


# ---------------------------------------------------------------------------
# Step 7: Main entrypoint — scrape all quests and write to JSON
# ---------------------------------------------------------------------------

def scrape_all_quests(output_path: str = "quests.json") -> list[dict]:
    """
    Fetch all quests from the OSRS Wiki, parse them, and write to a JSON file.

    Args:
        output_path: Where to write the output JSON file.

    Returns:
        The list of parsed quest dicts.
    """
    print("Fetching quest title list from category...")
    titles = get_quest_titles()
    print(f"Found {len(titles)} candidate pages in Category:Quests\n")

    quests = []
    for i, title in enumerate(titles, 1):
        print(f"[{i:>3}/{len(titles)}] Scraping: {title}")
        try:
            quest = scrape_quest(title)
            if quest:
                quests.append(quest)
        except Exception as e:
            print(f"  [ERROR] {title}: {e}")

    print(f"\nSuccessfully parsed {len(quests)} quests.")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(quests, f, indent=2, ensure_ascii=False)

    print(f"Written to {output_path}")
    return quests


# ---------------------------------------------------------------------------
# Developer helper: scrape a single named quest for rapid testing
# ---------------------------------------------------------------------------

def scrape_single(quest_name: str) -> dict | None:
    """
    Convenience function for testing/development.
    Example:
        from osrs_quest_scraper import scrape_single
        import json
        print(json.dumps(scrape_single("Priest in Peril"), indent=2))
    """
    return scrape_quest(quest_name)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape OSRS quest requirements and rewards from the wiki."
    )
    parser.add_argument(
        "--quest",
        type=str,
        default=None,
        help="Scrape a single quest by name (for testing). E.g. --quest 'Priest in Peril'",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="quests.json",
        help="Output JSON file path (default: quests.json)",
    )
    args = parser.parse_args()

    if args.quest:
        result = scrape_single(args.quest)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Could not parse quest: {args.quest}")
    else:
        scrape_all_quests(output_path=args.output)
