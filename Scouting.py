"""Scouting reports: numbers from the database in, a short written report out.

How it works, and why it is built this way:
  1. player_facts() works out every number from the database in plain Python.
     The model never does the math.
  2. generate_report() sends ONLY those numbers to Claude and asks for a short
     report that uses only them.
  3. unsupported_numbers() checks the finished report and lists any number that
     is not in the data, so a made-up figure gets flagged instead of trusted.

The Streamlit page (app.py) calls these three functions. You can also try it
from the terminal:   python3 scouting.py "Last name"
"""

import json
import re
import sys

from db import run_query

HITS = {"1B": 1, "2B": 2, "3B": 3, "HR": 4}  # result -> total bases
SMALL_SAMPLE = 20  # fewer plate appearances / pitches than this = "small sample"


def pct(part, whole):
    return round(100 * part / whole) if whole else None


def rate(part, whole):
    """A baseball-style average like 0.333, or None if there is nothing to divide."""
    return round(part / whole, 3) if whole else None


# ---------------------------------------------------------------------------
# Step 1: the facts
# ---------------------------------------------------------------------------

def hitting_facts(pid):
    rows = run_query(
        "SELECT game_id, result, final_balls, final_strikes, scored FROM at_bats WHERE hitter_id = %s",
        (pid,),
    )
    if not rows:
        return None
    pa = len(rows)
    result = lambda r: str(r["result"]).upper()
    counts = {}
    for r in rows:
        counts[result(r)] = counts.get(result(r), 0) + 1

    hits = [r for r in rows if result(r) in HITS]
    walks = sum(result(r) == "BB" for r in rows)
    hbp = sum(result(r) == "HBP" for r in rows)
    strikeouts = sum(result(r) == "K" for r in rows)
    reached_other = sum(result(r) == "FC" or re.fullmatch(r"E\d|ROE", result(r)) is not None for r in rows)
    in_play_outs = pa - len(hits) - walks - hbp - strikeouts - reached_other
    at_bats = pa - walks - hbp  # no way to see sacrifices on the chart, so this is approximate
    total_bases = sum(HITS[result(r)] for r in hits)

    facts = {
        "plate_appearances": pa,
        "games": len({r["game_id"] for r in rows}),
        "results_count": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "hits": len(hits),
        "singles": sum(result(r) == "1B" for r in rows),
        "doubles": sum(result(r) == "2B" for r in rows),
        "triples": sum(result(r) == "3B" for r in rows),
        "home_runs": sum(result(r) == "HR" for r in rows),
        "walks": walks,
        "hit_by_pitch": hbp,
        "strikeouts": strikeouts,
        "outs_on_balls_in_play": in_play_outs,
        "reached_on_error_or_fielders_choice": reached_other,
        "runs_scored": int(sum(bool(r["scored"]) for r in rows)),
        "batting_average": rate(len(hits), at_bats),
        "on_base_percentage": rate(len(hits) + walks + hbp, pa),
        "slugging_percentage": rate(total_bases, at_bats),
        "strikeout_percent": pct(strikeouts, pa),
        "walk_percent": pct(walks, pa),
    }

    # Count data: the ball/strike count when each at-bat ended (when the chart had it).
    counted = [r for r in rows if r["final_balls"] is not None and r["final_strikes"] is not None]
    if counted:
        two_strikes = [r for r in counted if r["final_strikes"] >= 2]
        three_balls = [r for r in counted if r["final_balls"] >= 3]
        first_pitch = [r for r in counted if r["final_balls"] == 0 and r["final_strikes"] == 0
                       and result(r) not in ("K", "BB", "HBP")]
        facts["count_data"] = {
            "plate_appearances_with_count": len(counted),
            "put_ball_in_play_on_first_pitch": len(first_pitch),
            "reached_two_strikes": len(two_strikes),
            "struck_out_after_reaching_two_strikes": sum(result(r) == "K" for r in two_strikes),
            "reached_three_balls": len(three_balls),
            "walked_after_reaching_three_balls": sum(result(r) == "BB" for r in three_balls),
            "average_balls_at_end": round(sum(r["final_balls"] for r in counted) / len(counted), 2),
            "average_strikes_at_end": round(sum(r["final_strikes"] for r in counted) / len(counted), 2),
        }
    if pa < SMALL_SAMPLE:
        facts["sample_size_note"] = f"small sample: only {pa} plate appearances"
    return facts


def pitching_facts(pid):
    abs_ = run_query("SELECT game_id, result FROM at_bats WHERE pitcher_id = %s", (pid,))
    pitches = run_query(
        """
        SELECT p.pitch_type, p.velocity, p.location_x, p.location_y, p.result
        FROM pitches p JOIN at_bats a ON p.at_bat_id = a.at_bat_id
        WHERE a.pitcher_id = %s
        """,
        (pid,),
    )
    if not abs_ and not pitches:
        return None
    faced = len(abs_)
    result = lambda r: str(r["result"]).upper()
    facts = {
        "batters_faced": faced,
        "games": len({r["game_id"] for r in abs_}),
        "strikeouts": sum(result(r) == "K" for r in abs_),
        "walks": sum(result(r) == "BB" for r in abs_),
        "hit_by_pitch": sum(result(r) == "HBP" for r in abs_),
        "hits_allowed": sum(result(r) in HITS for r in abs_),
        "home_runs_allowed": sum(result(r) == "HR" for r in abs_),
        "strikeout_percent": pct(sum(result(r) == "K" for r in abs_), faced),
        "walk_percent": pct(sum(result(r) == "BB" for r in abs_), faced),
        "pitches_thrown": len(pitches),
    }

    located = [p for p in pitches if p["location_x"] is not None and p["location_y"] is not None]
    in_zone = lambda p: 0 <= p["location_x"] <= 1 and 0 <= p["location_y"] <= 1
    if located:
        facts["pitches_with_location"] = len(located)
        facts["in_zone_percent"] = pct(sum(in_zone(p) for p in located), len(located))
        inside = [p for p in located if in_zone(p)]
        outside = [p for p in located if not in_zone(p)]
        if inside:  # which third of the zone: x and y run 0 to 1 across the zone
            third = lambda v: 0 if v < 1 / 3 else 1 if v < 2 / 3 else 2
            facts["in_zone_by_height"] = {name: sum(third(p["location_y"]) == i for p in inside)
                                          for i, name in enumerate(["low", "middle", "high"])}
            facts["in_zone_by_side"] = {name: sum(third(p["location_x"]) == i for p in inside)
                                        for i, name in enumerate(["left side of chart", "middle", "right side of chart"])}
        facts["outside_zone"] = {
            "total": len(outside),
            "above": sum(p["location_y"] > 1 for p in outside),
            "below": sum(p["location_y"] < 0 for p in outside),
            "left side of chart": sum(p["location_x"] < 0 for p in outside),
            "right side of chart": sum(p["location_x"] > 1 for p in outside),
        }

    mix = {}
    for p in pitches:
        mix.setdefault(p["pitch_type"] or "unknown", []).append(p)
    facts["pitch_mix"] = []
    for kind, group in sorted(mix.items(), key=lambda kv: -len(kv[1])):
        velos = [p["velocity"] for p in group if p["velocity"] is not None]
        with_loc = [p for p in group if p["location_x"] is not None and p["location_y"] is not None]
        entry = {"type": kind, "thrown": len(group), "share_percent": pct(len(group), len(pitches))}
        if velos:
            entry["average_velocity_mph"] = round(sum(velos) / len(velos), 1)
            entry["max_velocity_mph"] = round(max(velos), 1)
        if with_loc:
            entry["in_zone_percent"] = pct(sum(in_zone(p) for p in with_loc), len(with_loc))
        facts["pitch_mix"].append(entry)
    if len(pitches) < SMALL_SAMPLE:
        facts["sample_size_note"] = f"small sample: only {len(pitches)} pitches and {faced} batters faced"
    return facts


def player_facts(player):
    """Everything we know about one player (a row from the players table)."""
    facts = {"name": f"{player['first_name']} {player['last_name']}"}
    if player["role"] in ("hitter", "both"):
        hitting = hitting_facts(player["player_id"])
        if hitting:
            facts["hitting"] = hitting
    if player["role"] in ("pitcher", "both"):
        pitching = pitching_facts(player["player_id"])
        if pitching:
            facts["pitching"] = pitching
    return facts


def has_data(facts):
    return "hitting" in facts or "pitching" in facts


# ---------------------------------------------------------------------------
# Step 2: the report
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are a baseball analyst helping a coach prepare. You will get a JSON summary of one
player's charted data. Write a short scouting report using ONLY that data.

Rules:
- Use only numbers that appear in the data, written exactly as given. Never calculate a
  new number, estimate, or compare to league averages.
- Do not invent anything the data does not show: no pitch sequences, no game situations,
  no handedness, no physical traits, no "he tends to" claims that a number does not support.
- If the data has a sample_size_note, say so plainly and keep your conclusions tentative.
- Write in plain language, like a coach talking to a player. No hype.
- "left side of chart" and "right side of chart" are as drawn on the paper chart. Do not
  turn them into inside or outside.

Format (markdown). If the data has both hitting and pitching, write the sections twice,
under the headings "Hitting" and "Pitching".
**Summary** - two sentences.
**What stands out** - three or four bullets, each citing at least one number from the data.
**How to attack him** (for a hitter) or **What to expect from him** (for a pitcher) -
two or three practical bullets tied to the data.
**Caveats** - one sentence on sample size and what the charts do not capture.
Keep the whole report under 250 words.
""".strip()


def generate_report(client, facts):
    """Ask Claude for the report. Returns the text."""
    from extract_chart import MODEL  # same model setting as the chart reader

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        thinking={"type": "between_tools"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "Player data:\n" + json.dumps(facts, indent=2)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("The model returned no text.")
    return text


# ---------------------------------------------------------------------------
# Step 3: the check
# ---------------------------------------------------------------------------

NUMBER = re.compile(r"\d*\.?\d+")


def numbers_in(text):
    return {round(float(n), 3) for n in NUMBER.findall(text)}


def unsupported_numbers(report, facts):
    """Numbers that appear in the report but nowhere in the data, as written.
    An empty list means every number in the report can be found in the data."""
    allowed = numbers_in(json.dumps(facts))
    # Counting words never come from the data ("first pitch", "two strikes").
    found = []
    for raw in NUMBER.findall(report):
        value = round(float(raw), 3)
        if value not in allowed and raw not in found:
            found.append(raw)
    return found


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    matches = run_query(
        "SELECT * FROM players WHERE CONCAT(first_name, ' ', last_name) LIKE %s", (f"%{name}%",))
    if not matches:
        sys.exit(f"No player matches '{name}'.")
    facts = player_facts(matches[0])
    print(json.dumps(facts, indent=2))
    if has_data(facts) and "--write" in sys.argv:
        from extract_chart import make_client

        report = generate_report(make_client(), facts)
        print("\n" + report)
        extra = unsupported_numbers(report, facts)
        print("\nNumbers not found in the data:", extra or "none")