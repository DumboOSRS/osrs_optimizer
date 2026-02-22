# OSRS Completion Optimizer

## Project Overview

This project aims to find the most efficient path to "completing" Old School RuneScape (OSRS) on an **Ironman account**, where completion is defined as:
- Level 99 in all skills
- Obtaining all items in a pre-defined list

Since Ironman accounts cannot trade with other players, all items must be self-obtained. OSRS is an open-ended MMORPG with no set path — the solution space is computationally intractable and must be approximated.

---

## Problem Framing

The problem is modeled as a **directed dependency graph** with probabilistic nodes, where everything in OSRS is an `Action` with:
- A time cost (hours)
- A set of preconditions (skill levels, items, quests)
- A set of outputs (XP, items, quest completions, unlocks)

This reduces to a variant of the **Resource-Constrained Project Scheduling Problem (RCPSP)**, which is NP-hard. The approach is to use heuristics and metaheuristics to find near-optimal solutions.

### Core Action Schema

```json
{
  "id": "string",
  "type": "skill_training | boss_kill | quest | minigame | skilling_activity",
  "preconditions": {
    "skills": {"skill_name": min_level},
    "items": ["item_id"],
    "quests": ["quest_id"],
    "other": ["string"]
  },
  "outputs": {
    "xp": {"skill_name": xp_per_hour},
    "items": [{"item_id": "string", "rate": "items_per_hour", "drop_chance": "float"}],
    "quest_completion": "quest_id | null"
  },
  "time_cost_hours": "float",
  "notes": "string"
}
```

---

## Project Stages

### Stage 1 — Data Compilation (current)
Scrape and structure all required game data from the OSRS Wiki using the MediaWiki API.

### Stage 2 — Graph Construction
Load data into a dependency graph (NetworkX). Validate for unresolvable circular dependencies.

### Stage 3 — Baseline Heuristic
Greedy algorithm: at each step, take the highest XP/hr or item-value/hr action whose preconditions are currently met.

### Stage 4 — Optimization
Apply metaheuristics (simulated annealing, genetic algorithms) or constraint programming (Google OR-Tools) to improve on the greedy baseline.

---

## Data Sources

All game data is sourced from the **OSRS Wiki** (`oldschool.runescape.wiki`) via its MediaWiki API at:
```
https://oldschool.runescape.wiki/api.php
```

Key data categories to collect:
- **Quests** — requirements (skills, items, prerequisite quests) and rewards (XP, items, unlocks)
- **Skill training methods** — XP/hr by method, level requirements, item requirements
- **Bosses** — drop tables with drop rates, kills/hr by gear tier
- **Items** — the target completion list

---

## Repository Structure

```
osrs-optimizer/
├── CLAUDE.md                  # This file
├── environment.yml            # Conda environment
├── quests.json                # Scraped quest data (root-level output)
├── scripts/
│   └── osrs_quest_scraper.py  # Quest scraper (complete)
└── optimizer/                 # Graph construction + optimization (TODO)
```

---

## Environment Setup

```bash
conda env create -f environment.yml
conda activate osrs-optimizer
```

### Dependencies
- `python=3.11`
- `requests` — HTTP calls to the MediaWiki API
- `mwparserfromhell` — parses raw wikitext templates cleanly

---

## Scraper Usage

### Scrape all quests
```bash
python scripts/osrs_quest_scraper.py
# Output: quests.json
```

### Test a single quest
```bash
python scripts/osrs_quest_scraper.py --quest "Priest in Peril"
```

### Use as a module
```python
from scripts.osrs_quest_scraper import scrape_single
import json
print(json.dumps(scrape_single("Dragon Slayer I"), indent=2))
```

---

## Quest Data Schema

```json
{
  "id": "Cook's_Assistant",
  "name": "Cook's Assistant",
  "series": "None",
  "members": false,
  "quest_points": 1,
  "requirements": {
    "quests": [],
    "skills": [],
    "items": [
      {"item": "Egg (can be obtained during the quest)", "quantity": 1, "obtainable_in_quest": true},
      {"item": "Bucket of milk", "quantity": 1, "obtainable_in_quest": false},
      {"item": "Pot of flour", "quantity": 1, "obtainable_in_quest": false}
    ],
    "other": []
  },
  "rewards": {
    "quest_points": 1,
    "xp": [
      {"skill": "cooking", "amount": 300}
    ],
    "items": [
      {"item": "Permission to use the Cook-o-matic 100, which reduces the chance of burning some foods.", "quantity": 1}
    ],
    "unlocks": []
  }
}
```

---

## Scraper Design Notes

- **Rate limiting**: 0.5s delay between API requests + descriptive `User-Agent` header, per wiki bot policy
- **Wikitext parsing**: Uses `mwparserfromhell` to parse wiki templates; no brittle HTML scraping
- **Quest list**: Fetched from `Category:Quests` via `categorymembers` API (paginated), with meta-pages filtered out
- **Wiki template conventions** (confirmed by inspecting raw wikitext):
  - Quest metadata lives in `{{Infobox Quest}}` (series, members) — no requirements stored here
  - Requirements (skills, quest prereqs, items) are parameters of `{{Quest details}}`: `|requirements`, `|items`
  - Skill levels use `{{SCP|SkillName|Level|link=yes}}` ("Skill ClickPic"); boostability is set by an adjacent `{{Boostable|yes/no}}` on the same line
  - Quest prereqs are `[[WikiLink]]` items on their own bullet line inside `|requirements` (links with other prose on the line are incidental mentions, not prereqs)
  - Rewards live in `{{Quest rewards}}`: `|qp` for quest points, `|rewards` for the bullet list; XP rewards use `{{SCP|Skill|Amount}} [[experience]]`
- **Known limitations**:
  - XP lamp rewards with player-chosen skills (e.g. antique lamps) land in `rewards.items` and need a post-processing pass
  - Quest prerequisite detection is heuristic — cross-reference against known quest title list after scraping
  - Kills-per-hour for bosses will need to be modeled as a function of gear tier (3–4 tiers suggested)

---

## Key Simplifications for Tractability

1. **Discretize skill levels** at meaningful breakpoints: 1, 40, 60, 70, 75, 80, 85, 90, 99
2. **Phase separation**: early game (questing for XP), mid game (unlocking efficient methods), late game (bossing for BiS, 99s)
3. **Expected drop rates**: model boss drops probabilistically using expected kills, not specific RNG outcomes
4. **Ignore micro-optimization**: focus on which training method to use and when to switch, not tick manipulation
5. **Opportunity cost modeling**: every hour bossing is an hour not skilling, but may produce gear that multiplies future efficiency
