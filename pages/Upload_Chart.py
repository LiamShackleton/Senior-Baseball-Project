"""Upload page: photo in, reviewed data saved to the database.

Streamlit shows every file in the pages/ folder as its own page in the sidebar,
next to app.py (the stats page).

The steps on this page:
  1. Pick the chart type and upload a photo.
  2. Check the layout preview (no cost), then press "Read chart" (uses the API).
  3. Review and fix what was read, in editable tables.
  4. Choose the game and who the players are, then save.

All the reading and saving code is shared with the command-line scripts
(extract_chart.py, extract_pitching_chart.py, import_chart.py,
import_pitching.py), so both routes behave the same.
"""

import copy
import hashlib
import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

import extract_chart as hit
import extract_pitching_chart as pit
import import_chart as imp_h
import import_pitching as imp_p
from db import run_query

st.set_page_config(page_title="Upload a chart", layout="wide")
st.title("📷 Upload a chart")

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "uploaded_charts"
KINDS = {"Hitting chart (at-bats)": "hitting", "Pitching chart": "pitching"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def none_if_blank(value):
    return None if pd.isna(value) else value


def to_int(value):
    value = none_if_blank(value)
    return None if value is None else int(value)


def save_upload(uploaded):
    """Write the uploaded photo to uploaded_charts/ and return its relative path.
    The file name includes a short hash of the contents, so uploading the same
    photo twice gives the same path and the duplicate check can catch it."""
    data = uploaded.getvalue()
    digest = hashlib.sha1(data).hexdigest()[:10]
    UPLOAD_DIR.mkdir(exist_ok=True)
    path = UPLOAD_DIR / f"{digest}_{Path(uploaded.name).name}"
    if not path.exists():
        path.write_bytes(data)
    return path.relative_to(ROOT).as_posix()


def choose_player(label, name, role, key):
    """Let the user say who a chart name is. Returns a decision in the same
    form the command-line importers use:
      ("use", player_id, current_role, needed_role) / ("create", first, last, role) / ("skip",)"""
    matches = imp_h.find_players(name) if name else []
    options = {"new": "Create a new player"}
    for p in matches:
        options[f"id:{p['player_id']}"] = f"{imp_h.full_name(p)} ({p['role']}, id {p['player_id']})"
    options["skip"] = "Skip (don't save)"
    default = f"id:{matches[0]['player_id']}" if len(matches) == 1 else "new"
    choice = st.selectbox(label, list(options), index=list(options).index(default),
                          format_func=options.get, key=f"{key}_choice")
    if choice == "skip":
        return ("skip",)
    if choice == "new":
        left, right = st.columns(2)
        first = left.text_input("First name", value=name, key=f"{key}_first")
        last = right.text_input("Last name (optional)", key=f"{key}_last")
        return ("create", first.strip() or name, last.strip(), role)
    player = next(p for p in matches if f"id:{p['player_id']}" == choice)
    return ("use", player["player_id"], player["role"], role)


def choose_game(key):
    """Returns a game plan: ("existing", id) or ("new", date, opponent, location)."""
    games = run_query("SELECT game_id, game_date, opponent FROM games "
                      "ORDER BY game_date DESC, game_id DESC LIMIT 50")
    mode = st.radio("Game", ["New game", "Add to an existing game"], horizontal=True,
                    key=f"{key}_gamemode", label_visibility="collapsed")
    if mode == "Add to an existing game":
        if not games:
            st.info("No games in the database yet, so a new one will be created.")
        else:
            labels = {g["game_id"]: f"{g['game_date']} vs {g['opponent'] or '?'} (id {g['game_id']})"
                      for g in games}
            return ("existing", st.selectbox("Which game?", list(labels),
                                             format_func=labels.get, key=f"{key}_game"))
    a, b, c = st.columns(3)
    when = a.date_input("Date", value=date.today(), key=f"{key}_date")
    opponent = b.text_input("Opponent", key=f"{key}_opp")
    location = c.text_input("Location", key=f"{key}_loc")
    return ("new", when.isoformat(), opponent.strip() or None, location.strip() or None)


def duplicate_check(image, key):
    """True if it's fine to go ahead (not imported before, or the user said so)."""
    previous = run_query("SELECT chart_id, uploaded_at FROM source_charts WHERE image_path = %s",
                         (image,))
    if not previous:
        return True
    st.warning(f"This photo was already imported (chart {previous[0]['chart_id']}, "
               f"{previous[0]['uploaded_at']}). Saving it again would duplicate everything.")
    return st.checkbox("Save it again anyway", key=f"{key}_force")


def show_warnings(warnings, limit=12):
    if not warnings:
        st.success("No consistency problems found.")
        return
    with st.expander(f"{len(warnings)} things to check", expanded=len(warnings) <= limit):
        for w in warnings[:limit]:
            st.write("•", w)
        if len(warnings) > limit:
            st.caption(f"... and {len(warnings) - limit} more.")


# ---------------------------------------------------------------------------
# Hitting charts
# ---------------------------------------------------------------------------

def hitting_settings(path):
    """Layout controls plus a free preview of how the photo will be cut."""
    with st.expander("Layout settings (check the preview before reading)", expanded=True):
        a, b, c = st.columns(3)
        hit.ROWS = a.number_input("Batters on the sheet", 1, 20, hit.ROWS)
        hit.COLUMNS = b.number_input("At-bat boxes per row", 1, 12, hit.COLUMNS)
        hit.ROTATE = c.selectbox("Rotate clockwise (degrees)", [0, 90, 180, 270],
                                 index=[0, 90, 180, 270].index(hit.ROTATE))
        d, e = st.columns(2)
        hit.GRID_TOP = d.slider("Where the first batter row starts", 0.0, 0.5, hit.GRID_TOP, 0.005)
        hit.GRID_BOTTOM = e.slider("Where the last batter row ends", 0.5, 1.0, hit.GRID_BOTTOM, 0.005)
        st.image(hit.overlay_image(hit.load_image(ROOT / path, hit.ROTATE)),
                 caption="Each red band should hold exactly one batter. Adjust the sliders "
                         "until it does. This preview is free.", width=380)


def hitting_review(state):
    result, original = state["result"], state["original"]
    st.subheader("2. Review what was read")
    st.caption("Fix anything that's wrong. Leave Result empty for a box with no at-bat.")
    names, frames = {}, {}
    for number, batter in enumerate(result["batters"], start=1):
        rows = [{"box": ab.get("column"), "result": ab.get("result") or "",
                 "balls": ab.get("balls"), "strikes": ab.get("strikes"),
                 "scored": bool(ab.get("scored"))} for ab in batter.get("at_bats", [])]
        df = pd.DataFrame(rows, columns=["box", "result", "balls", "strikes", "scored"])
        df["balls"] = pd.array(df["balls"], dtype="Int64")
        df["strikes"] = pd.array(df["strikes"], dtype="Int64")
        flag = " ⚠️" if batter.get("extraction_error") else ""
        with st.expander(f"{number}. {batter.get('name') or '?'}{flag}", expanded=False):
            if batter.get("extraction_error"):
                st.error(f"This row failed to read: {batter['extraction_error']}")
            names[number] = st.text_input("Name", value=batter.get("name") or "",
                                          key=f"h_name_{number}_{state['id']}")
            frames[number] = st.data_editor(
                df, hide_index=True, disabled=["box"], key=f"h_ed_{number}_{state['id']}",
                width="stretch",
                column_config={
                    "box": st.column_config.NumberColumn("Box"),
                    "result": st.column_config.TextColumn("Result"),
                    "balls": st.column_config.NumberColumn("Balls", min_value=0, max_value=4, step=1),
                    "strikes": st.column_config.NumberColumn("Strikes", min_value=0, max_value=5, step=1),
                    "scored": st.column_config.CheckboxColumn("Scored"),
                })

    # Put the edits back into a result in the same shape the importer expects.
    edited = copy.deepcopy(result)
    for number, batter in enumerate(edited["batters"], start=1):
        batter["name"] = names[number].strip()
        at_bats = []
        for _, row in frames[number].iterrows():
            text = " ".join(str(row["result"] or "").upper().split())
            at_bats.append({"column": to_int(row["box"]), "box_blank": not text,
                            "balls": to_int(row["balls"]), "strikes": to_int(row["strikes"]),
                            "result": text or None, "scored": bool(row["scored"]),
                            "uncertain": False})
        batter["at_bats"] = at_bats
        if batter.get("extraction_error") and any(a["result"] for a in at_bats):
            del batter["extraction_error"]  # you filled it in by hand

    show_warnings(hit.check_result(edited))

    st.subheader("3. Game and players")
    game_plan = choose_game("h")
    team = st.text_input("Team name for any NEW players (optional)", key="h_team")

    decisions, plans = {}, {}
    unique = {}
    for number, batter in enumerate(edited["batters"], start=1):
        name = (batter.get("name") or "").strip()
        if batter.get("extraction_error") or not name or name.startswith("ROW "):
            continue
        unique.setdefault(name.lower(), (name, []))[1].append(number)
    for key, (name, numbers) in unique.items():
        with st.container(border=True):
            decisions[key] = choose_player(f"Who is '{name}'?", name, "hitter", f"h_pl_{key}")
        for number in numbers:
            plans[number] = decisions[key]

    saveable = sum(1 for number, b in enumerate(edited["batters"], start=1)
                   if plans.get(number, ("skip",))[0] != "skip"
                   for ab in b["at_bats"] if imp_h.saveable(ab))
    st.subheader("4. Save")
    st.write(f"**{saveable} at-bats** will be saved. Batters with no name, or set to "
             f"Skip, are left out. Boxes with no result are left out.")
    ok = duplicate_check(state["path"], "h")
    if st.button("Save to the database", type="primary", disabled=not (ok and saveable)):
        try:
            game_id, chart_id, count = imp_h.save(edited, original, game_plan, plans,
                                                  state["path"], team.strip() or None)
        except Exception as error:
            st.error(f"Something went wrong, so everything was undone. Nothing was saved.\n\n{error}")
            return
        finish(f"Saved {count} at-bats (game {game_id}, chart {chart_id}).")


# ---------------------------------------------------------------------------
# Pitching charts
# ---------------------------------------------------------------------------

def pitching_settings(path):
    with st.expander("Layout settings (check the preview before reading)", expanded=True):
        pit.SPLIT = st.slider("Where the velocity table ends and the zone boxes begin",
                              0.2, 0.7, pit.SPLIT, 0.01)
        table_img, zones_img = pit.split_halves(pit.load_image(ROOT / path, pit.ROTATE))
        left, right = st.columns(2)
        left.image(table_img, caption="Velocity table", width="stretch")
        right.image(zones_img, caption="Zone boxes", width="stretch")
        st.caption("The left picture should show only the velocity table and the right one "
                   "only the zone boxes. This preview is free.")


def pitching_review(state):
    result, original = state["result"], state["original"]
    st.subheader("2. Review what was read")
    show_warnings(state.get("warnings", []))
    st.caption("Fix anything that's wrong. 'Where' says where the pitch landed relative to "
               "the black strike-zone outline.")

    edited = copy.deepcopy(result)
    for box in edited["boxes"]:
        order = box["order"]
        with st.expander(f"At-bat {order}: {box.get('result') or 'no result'}  "
                         f"(pitches {box['pitches'][0]['number']}-{box['pitches'][-1]['number']})"):
            box["result"] = st.text_input("Result (K, BB, F7, 1B, ...)", value=box.get("result") or "",
                                          key=f"p_res_{order}_{state['id']}").strip().upper() or None
            df = pd.DataFrame([{"pitch": p["number"], "type": p.get("pitch_type") or "",
                                "velocity": p.get("velocity"), "where": imp_p.where_of(p)}
                               for p in box["pitches"]])
            df["velocity"] = pd.to_numeric(df["velocity"])
            frame = st.data_editor(
                df, hide_index=True, disabled=["pitch"], key=f"p_ed_{order}_{state['id']}",
                width="stretch",
                column_config={
                    "pitch": st.column_config.NumberColumn("Pitch #"),
                    "type": st.column_config.TextColumn("Type"),
                    "velocity": st.column_config.NumberColumn("Velocity", min_value=40, max_value=110),
                    "where": st.column_config.SelectboxColumn("Where", options=list(imp_p.PLACES)),
                })
            for pitch, (_, row) in zip(box["pitches"], frame.iterrows()):
                pitch["pitch_type"] = str(row["type"]).strip().upper() or None
                velocity = none_if_blank(row["velocity"])
                pitch["velocity"] = None if velocity is None else float(velocity)
                if row["where"] != imp_p.where_of(pitch):   # changed by hand
                    pitch["where"] = row["where"]
                    pitch["x"], pitch["y"] = imp_p.PLACES[row["where"]]

    st.subheader("3. Pitcher and game")
    name = st.text_input("Pitcher's name", value=(result.get("pitcher") or ""), key="p_name")
    with st.container(border=True):
        pitcher = choose_player(f"Who is '{name or '...'}'?", name.strip(), "pitcher", "p_pl")
    game_plan = choose_game("p")
    team = st.text_input("Team name for a NEW pitcher (optional)", key="p_team")

    saveable = [b for b in edited["boxes"] if b.get("result")]
    n_pitches = sum(len(b["pitches"]) for b in saveable)
    st.subheader("4. Save")
    st.write(f"**{len(saveable)} at-bats and {n_pitches} pitches** will be saved "
             f"(batters aren't recorded on pitching charts).")
    if len(saveable) < len(edited["boxes"]):
        st.warning("At-bats with no result are left out. Type a result above to include them.")
    ok = duplicate_check(state["path"], "p")
    can_save = ok and bool(saveable) and bool(name.strip()) and pitcher[0] != "skip"
    if st.button("Save to the database", type="primary", disabled=not can_save):
        try:
            game_id, chart_id, at_bats, pitches = imp_p.save(
                edited, original, game_plan, pitcher, state["path"], team.strip() or None)
        except Exception as error:
            st.error(f"Something went wrong, so everything was undone. Nothing was saved.\n\n{error}")
            return
        finish(f"Saved {at_bats} at-bats and {pitches} pitches (game {game_id}, chart {chart_id}).")


# ---------------------------------------------------------------------------
# Page flow
# ---------------------------------------------------------------------------

def finish(message):
    st.session_state["done"] = message
    st.session_state.pop("chart", None)
    st.rerun()


if st.session_state.get("done"):
    st.success(st.session_state["done"])
    try:
        st.page_link("app.py", label="See the stats", icon="📊")
    except Exception:
        st.write("Open the stats page from the sidebar to see it.")
    if st.button("Upload another chart"):
        st.session_state.pop("done", None)
        st.rerun()
    st.stop()

state = st.session_state.get("chart")

if state is None:
    st.subheader("1. Upload a photo")
    kind = KINDS[st.radio("What kind of chart is it?", list(KINDS), horizontal=True)]
    uploaded = st.file_uploader("Photo of the chart (flat, upright and evenly lit works best)",
                                type=["png", "jpg", "jpeg"])
    if uploaded is None:
        st.info("Choose a photo to get started.")
        st.stop()

    path = save_upload(uploaded)
    (hitting_settings if kind == "hitting" else pitching_settings)(path)

    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_key:
        st.error("No ANTHROPIC_API_KEY found. Check your .env file, then restart the app.")
    note = ("about " + str(hit.ROWS) + " requests, which can take a few minutes"
            if kind == "hitting" else "2 requests")
    if st.button("Read chart", type="primary", disabled=not has_key):
        try:
            if kind == "hitting":
                bar = st.progress(0.0, text="Reading rows...")
                result = hit.read_hitting_chart(
                    ROOT / path,
                    progress=lambda done, total: bar.progress(
                        done / total, text=f"Read {done} of {total} rows"))
                warnings = hit.check_result(result)
            else:
                with st.spinner("Reading the velocity table and the zone boxes..."):
                    result, warnings = pit.read_pitching_chart(ROOT / path)
        except Exception as error:
            st.error(f"Couldn't read the chart: {error}")
            st.stop()
        st.session_state["chart"] = {"kind": kind, "path": path, "result": result,
                                     "original": copy.deepcopy(result),
                                     "warnings": warnings, "id": os.urandom(4).hex()}
        st.rerun()
    st.caption(f"Reading uses your Claude API key ({note}).")
else:
    top, button = st.columns([5, 1])
    top.caption(f"Reading of **{state['path']}**")
    if button.button("Start over"):
        st.session_state.pop("chart", None)
        st.rerun()
    (hitting_review if state["kind"] == "hitting" else pitching_review)(state)