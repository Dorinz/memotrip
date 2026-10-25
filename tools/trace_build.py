#!/usr/bin/env python3
"""
trace_build.py — run one trip through the build pipeline, saving every intermediate.

Mirrors webapp.run_build() step by step (minus the DB, the background queue and
the photo step), writing each stage's output to a numbered file, plus the exact
prompt of every model call and the raw JSON it returned. Meant for reading the
flow on a concrete example, not for producing pages.

    python tools/trace_build.py --name azores \
        --description benchmark/azores/description.txt --docs benchmark/azores/docs/*

Writes traces/<name>/ (git-ignored). Needs GEMINI_API_KEY (.env) for the model
steps; without it they're skipped, the same as in the webapp.

If run_build() changes, update the steps here to match.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import local_env  # noqa: F401  (loads .env)
except Exception:
    pass

import build_trip  # noqa: E402
import gemini_util  # noqa: E402
import gen_copy  # noqa: E402
import palette  # noqa: E402
import parse_docs  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MODEL_CALL_NAMES = {
    id(parse_docs.AI_SCHEMA): "call1_extract",
    id(gen_copy.TRIP_SCHEMA): "call2_trip_copy",
    id(gen_copy.DAYS_SCHEMA): "call3_day_copy",
}


class Trace:
    def __init__(self, out: pathlib.Path):
        self.out = out
        self.lock = threading.Lock()
        self.log_lines: list[str] = []
        self.calls: list[dict] = []

    def text(self, name: str, s: str):
        (self.out / name).write_text(s, encoding="utf-8")

    def json(self, name: str, obj):
        self.text(name, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

    def log(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line)
        with self.lock:
            self.log_lines.append(line)

    def step(self, title: str):
        self.log("")
        self.log(f"=== {title} ===")


def record_model_calls(tr: Trace):
    """Wrap gemini_util.generate_json so every call's prompt + answer is saved.
    parse_docs and gen_copy import gemini_util inside their functions, so they
    pick up the wrapper."""
    real = gemini_util.generate_json

    def wrapped(model, contents, *, schema=None, **kw):
        name = MODEL_CALL_NAMES.get(id(schema), "call_coords")
        t0 = time.time()
        tr.log(f"-> model call {name} ({len(str(contents))} chars of prompt)")
        try:
            resp = real(model, contents, schema=schema, **kw)
            err = None
        except Exception as e:
            resp, err = None, f"{type(e).__name__}: {e}"
        took = time.time() - t0
        prompt = contents if isinstance(contents, str) else repr(contents)
        tr.text(f"model/{name}.prompt.txt", prompt)
        tr.json(f"model/{name}.response.json", resp if err is None else {"error": err})
        with tr.lock:
            tr.calls.append(
                {"name": name, "seconds": round(took, 1), "prompt_chars": len(prompt), "error": err}
            )
        tr.log(f"<- model call {name} done in {took:.1f}s" + (f" - FAILED: {err}" if err else ""))
        if err:
            raise RuntimeError(err)
        return resp

    gemini_util.generate_json = wrapped


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", required=True, help="folder name under traces/")
    ap.add_argument("--description", default="", help="a file path or the text itself")
    ap.add_argument("--docs", nargs="*", default=[])
    ap.add_argument("--region-hint", default="")
    ap.add_argument("--model", default=None, help="default: the webapp's MODEL")
    a = ap.parse_args()

    import os

    model = a.model or os.environ.get("TRIP_MODEL", "gemini-3.6-flash")
    out = ROOT / "traces" / a.name
    if out.exists():
        shutil.rmtree(out)
    (out / "model").mkdir(parents=True)
    (out / "input" / "docs").mkdir(parents=True)
    tr = Trace(out)
    record_model_calls(tr)
    key = gemini_util.have_key()
    t_start = time.time()

    # ------------------------------------------------ A. intake (POST /trips)
    tr.step("A. input, as the user submitted it")
    desc_p = pathlib.Path(a.description)
    is_file = a.description and desc_p.is_file()
    desc = desc_p.read_text(encoding="utf-8") if is_file else a.description
    tr.text("input/description.txt", desc)
    files = []
    for d in a.docs:
        p = pathlib.Path(d)
        shutil.copy(p, out / "input" / "docs" / p.name)
        files.append(out / "input" / "docs" / p.name)
    tr.log(
        f"description: {len(desc)} chars; docs: {[f.name for f in files]};"
        f" region hint: {a.region_hint!r}"
    )

    # ------------------------------------------------ 6. read the documents
    tr.step("6. documents -> text")
    per_doc = {}
    for f in sorted(files):
        per_doc[f.name] = parse_docs.read_doc(f)
        tr.text(f"06_doc_text__{f.stem}.txt", per_doc[f.name])
        tr.log(f"{f.name}: {len(per_doc[f.name])} chars")
    text = "\n\n".join(per_doc[f.name] for f in sorted(files))
    tr.text("06_docs_text_joined.txt", text)

    # ------------------------------------------------ 7. parse the itinerary
    tr.step("7. itinerary: regex + model call 1")
    rx = parse_docs.parse_docx_regex(text + ("\n\n" + desc if desc else ""))
    tr.json("07a_regex_result.json", rx)
    tr.log(
        f"regex: {len(rx['flights'])} flights / {len(rx['ferries'])} ferries /"
        f" {len(rx['stays'])} stays, year={rx['year']}"
    )
    source = (desc + "\n\n" if desc else "") + text
    tr.log(f"model input: {len(source)} chars (cut at {parse_docs.MAX_DOC_CHARS})")
    ai = parse_docs.parse_ai(source, model, log=tr.log, rx=rx) if key else None
    tr.json("07b_model_extract_result.json", ai)

    # ------------------------------------------------ 8. assemble + split into days
    tr.step("8. assemble the skeleton, split stays into days")
    spec = parse_docs.assemble(ai, rx)
    from_model = ai and ai.get("timeline")
    tr.log("built from: " + ("the model's timeline" if from_model else "the regex result"))
    for w in spec.get("_warnings", []):
        tr.log("! model-reported " + w)
    for w in parse_docs.check_consistency(spec):
        tr.log("! consistency: " + w)
    tr.json("08a_assembled.json", spec)
    for it in spec["timeline"]:
        if it.get("type") in ("stay", "layover") and it.get("key"):
            spec.setdefault("photos", {}).setdefault(it["key"], {"lodging": [], "trip": []})
    spec = parse_docs.expand_days(spec)
    tr.json("08b_days_expanded.json", spec)
    tr.log(f"timeline: {len(spec['timeline'])} items after the day split")
    if not spec.get("timeline"):
        tr.log("EMPTY timeline - the webapp stops here with an error")
        return 1

    # ------------------------------------------------ 9. theme
    tr.step("9. color theme")
    spec["theme"] = palette.generate_palette(a.name)
    tr.json("09_theme.json", spec["theme"])

    # ------------------------------------------------ 10. coordinates
    tr.step("10. map coordinates")
    before = {k: dict(v) for k, v in spec.get("locations", {}).items()}
    cache = out / "geocode_cache.json"  # a copy - never touch the repo's cache
    if (ROOT / "geocode_cache.json").exists():
        shutil.copy(ROOT / "geocode_cache.json", cache)
    need = [v["label"] for v in before.values() if not v.get("lat")]
    tr.log(f"{len(before)} locations, {len(need)} without coordinates from the model: {need}")
    unresolved = parse_docs.fill_coords(spec, a.region_hint, model, cache, log=tr.log)
    tr.json(
        "10_locations.json",
        {"before": before, "after": spec.get("locations"), "unresolved": unresolved},
    )

    # ------------------------------------------------ 11. copy (calls 2 + 3)
    tr.step("11. prose: model calls 2 + 3 (in parallel)")
    tr.text("11a_digest.txt", gen_copy.digest(spec))
    if key:
        gen_copy.generate_copy(spec, desc or "", text, model=model, log=tr.log)
    else:
        tr.log("no GEMINI_API_KEY - prose skipped")
    tr.json("11b_after_copy.json", spec)

    # stat chips come from model call 2 (hero.meta / outro.stats) - saved here on their own
    tr.json("12_stats.json", {
        "hero.meta": spec.get("hero", {}).get("meta"),
        "outro.stats": spec.get("outro", {}).get("stats"),
    })

    # ------------------------------------------------ 13. page
    tr.step("13. render the page")
    tr.json("13_spec_final.json", spec)
    html = build_trip.build(spec, (ROOT / "trip_template.html").read_text(encoding="utf-8"))
    tr.text("13_page.html", html.replace("\r\n", "\n"))
    tr.log(f"page.html: {len(html)} chars; warnings: {spec.get('_warnings') or 'none'}")

    tr.step("summary")
    tr.log(f"total {time.time() - t_start:.1f}s; model calls: {tr.calls}")
    tr.json("calls.json", tr.calls)
    tr.text("log.txt", "\n".join(tr.log_lines) + "\n")
    print(f"\ntrace written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
