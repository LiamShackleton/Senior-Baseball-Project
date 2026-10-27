"""Score the chart reader against answer keys you typed from the photos.

    python3 evaluate.py --make-key extracted/TestChart_03.json   # start a key from an extraction
    python3 evaluate.py                                          # score every key in answer_keys/

How to use it:
  1. Run a chart through extraction (the upload page saves nothing here, so use
     extract_chart.py or extract_pitching_chart.py once per chart you want scored).
  2. --make-key writes answer_keys/<photo name>.json pre-filled with what the model
     read. OPEN THE PHOTO and correct every value in that file so it matches what
     is really written. A key you haven't checked against the photo proves nothing.
  3. Run evaluate.py. It compares each key to the saved extraction (no API calls,
     no cost) and prints accuracy numbers and every mistake.

Key formats (made for you by --make-key):
  hitting:  {"type": "hitting", "batters": [{"name": "Tate", "at_bats": [
              {"result": "K", "balls": 1, "strikes": 3, "scored": false}, ...]}]}
  pitching: {"type": "pitching", "boxes": [{"result": "K", "pitches": [
              {"number": 1, "pitch_type": "FB", "velocity": 86, "inside": true}, ...]}]}
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

KEY_DIR = Path("answer_keys")
EXTRACT_DIR = Path("extracted")  # change with --extractions


def norm(text):
    return " ".join(str(text or "").upper().split())


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def raw_count(ab):
    """The count from the model's first read, before the zoomed-in recount and
    before the walk/strikeout correction."""
    original = ab.get("first_pass_count") or ab.get("original_count")
    if original:
        balls, _, strikes = str(original).partition("-")
        return to_int(balls), to_int(strikes)
    return ab.get("balls"), ab.get("strikes")


def inside(pitch):
    x, y = pitch.get("x"), pitch.get("y")
    return x is not None and y is not None and 0 <= x <= 1 and 0 <= y <= 1


class Score:
    """Counts right/total for each thing being checked, and keeps the mistakes."""

    def __init__(self):
        self.right, self.total, self.mistakes = Counter(), Counter(), []

    def check(self, field, ok, where, expected, got):
        self.total[field] += 1
        if ok:
            self.right[field] += 1
        else:
            self.mistakes.append(f"{where}: {field} should be {expected}, got {got}")

    def merge(self, other):
        self.right.update(other.right)
        self.total.update(other.total)
        self.mistakes += other.mistakes


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_hitting(key, got):
    score = Score()
    got_batters = got.get("batters", [])
    for i, kb in enumerate(key["batters"]):
        gb = got_batters[i] if i < len(got_batters) else {"name": None, "at_bats": []}
        score.check("batter names", norm(kb["name"]) == norm(gb.get("name")),
                    f"batter {i + 1}", kb["name"], gb.get("name"))
        got_abs = gb.get("at_bats", [])
        for j, kab in enumerate(kb["at_bats"]):
            gab = got_abs[j] if j < len(got_abs) else {}
            if gab.get("box_blank"):
                gab = {}
            where = f"{kb['name']} box {j + 1}"
            score.check("at-bat results", norm(kab["result"]) == norm(gab.get("result")),
                        where, kab["result"], gab.get("result"))
            expected = (kab.get("balls"), kab.get("strikes"))
            score.check("counts (after correction)",
                        expected == (gab.get("balls"), gab.get("strikes")),
                        where, "%s-%s" % expected, "%s-%s" % (gab.get("balls"), gab.get("strikes")))
            score.check("counts (first read only)", expected == raw_count(gab),
                        where, "%s-%s" % expected, "%s-%s" % raw_count(gab))
            score.check("scored", bool(kab.get("scored")) == bool(gab.get("scored")),
                        where, bool(kab.get("scored")), bool(gab.get("scored")))
        invented = sum(1 for ab in got_abs[len(kb["at_bats"]):]
                       if not ab.get("box_blank") and ab.get("result"))
        score.check("no invented at-bats", invented == 0, kb["name"], "none",
                    f"{invented} extra")
    score.check("batter rows found", len(got_batters) == len(key["batters"]), "sheet",
                len(key["batters"]), len(got_batters))
    return score


def score_pitching(key, got):
    score = Score()
    got_boxes = got.get("boxes", [])
    for i, kb in enumerate(key["boxes"], start=1):
        gb = got_boxes[i - 1] if i <= len(got_boxes) else {"result": None, "pitches": []}
        score.check("at-bat results", norm(kb.get("result")) == norm(gb.get("result")),
                    f"at-bat {i}", kb.get("result"), gb.get("result"))
        score.check("pitches per at-bat", len(kb["pitches"]) == len(gb["pitches"]),
                    f"at-bat {i}", len(kb["pitches"]), len(gb["pitches"]))
        got_by_number = {p["number"]: p for p in gb["pitches"]}
        for kp in kb["pitches"]:
            gp = got_by_number.get(kp["number"], {})
            where = f"pitch {kp['number']}"
            score.check("pitch types", norm(kp.get("pitch_type")) == norm(gp.get("pitch_type")),
                        where, kp.get("pitch_type"), gp.get("pitch_type"))
            score.check("velocities", kp.get("velocity") == gp.get("velocity"),
                        where, kp.get("velocity"), gp.get("velocity"))
            if "inside" in kp:
                score.check("inside/outside the zone",
                            bool(kp["inside"]) == inside(gp) if gp else False,
                            where, "inside" if kp["inside"] else "outside",
                            ("inside" if inside(gp) else "outside") if gp else "missing")
    score.check("at-bats found", len(got_boxes) == len(key["boxes"]), "sheet",
                len(key["boxes"]), len(got_boxes))
    return score


# ---------------------------------------------------------------------------
# Making a starter key
# ---------------------------------------------------------------------------

def make_key(extraction_path):
    got = json.loads(Path(extraction_path).read_text())
    if "batters" in got:
        key = {"type": "hitting", "batters": [
            {"name": b.get("name"), "at_bats": [
                {"result": ab.get("result"), "balls": ab.get("balls"),
                 "strikes": ab.get("strikes"), "scored": bool(ab.get("scored"))}
                for ab in b.get("at_bats", []) if not ab.get("box_blank")]}
            for b in got["batters"]]}
        stem = Path(extraction_path).stem
    elif "boxes" in got:
        key = {"type": "pitching", "boxes": [
            {"result": box.get("result"), "pitches": [
                {"number": p["number"], "pitch_type": p.get("pitch_type"),
                 "velocity": p.get("velocity"), "inside": inside(p)}
                for p in box["pitches"]]}
            for box in got["boxes"]]}
        stem = Path(extraction_path).stem.removesuffix("_pitching")
    else:
        sys.exit("That doesn't look like an extraction file.")
    KEY_DIR.mkdir(exist_ok=True)
    out = KEY_DIR / f"{stem}.json"
    if out.exists():
        sys.exit(f"{out} already exists. Delete it first if you want to start over.")
    out.write_text(json.dumps(key, indent=2))
    print(f"Wrote {out}.\nNow open the PHOTO and correct every value in that file so it "
          f"matches what is really written, then run: python3 evaluate.py")


# ---------------------------------------------------------------------------

def percent(right, total):
    return f"{right}/{total}  ({100 * right / total:.0f}%)" if total else "-"


def print_score(title, score, show_mistakes):
    print(f"\n=== {title} ===")
    for field in score.total:
        print(f"  {field:<30} {percent(score.right[field], score.total[field])}")
    if show_mistakes and score.mistakes:
        print(f"  Mistakes ({len(score.mistakes)}):")
        for line in score.mistakes[:25]:
            print("   -", line)
        if len(score.mistakes) > 25:
            print(f"   ... and {len(score.mistakes) - 25} more")


def main():
    parser = argparse.ArgumentParser(description="Score extractions against answer keys.")
    parser.add_argument("--make-key", metavar="EXTRACTION_JSON",
                        help="start an answer key from an extraction file")
    parser.add_argument("--extractions", default="extracted",
                        help="folder with the extraction files to score (default: extracted). "
                             "Copy 'extracted' to another folder to compare settings.")
    args = parser.parse_args()
    global EXTRACT_DIR
    EXTRACT_DIR = Path(args.extractions)
    if args.make_key:
        make_key(args.make_key)
        return

    keys = sorted(KEY_DIR.glob("*.json"))
    if not keys:
        sys.exit(f"No answer keys in {KEY_DIR}/. Make one with --make-key.")

    totals = {"hitting": Score(), "pitching": Score()}
    for path in keys:
        key = json.loads(path.read_text())
        kind = key.get("type")
        if kind not in totals:
            print(f"Skipping {path.name}: no 'type' of hitting or pitching.")
            continue
        suffix = "_pitching" if kind == "pitching" else ""
        extraction = EXTRACT_DIR / f"{path.stem}{suffix}.json"
        if not extraction.exists():
            print(f"Skipping {path.name}: no extraction at {extraction}.")
            continue
        got = json.loads(extraction.read_text())
        score = (score_hitting if kind == "hitting" else score_pitching)(key, got)
        print_score(f"{path.stem} ({kind})", score, show_mistakes=True)
        totals[kind].merge(score)

    for kind, score in totals.items():
        if score.total:
            print_score(f"ALL {kind.upper()} CHARTS COMBINED", score, show_mistakes=False)


if __name__ == "__main__":
    main()