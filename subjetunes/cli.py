"""Command line entry points. The UI launches long jobs through these as subprocesses
(robust on Windows, where multiprocessing inside Streamlit is fragile), and you can
run them headless overnight.

  python -m subjetunes.cli info
  python -m subjetunes.cli analyze --xml rekordbox.xml --playlist "Folder/My List" [--no-clap]
  python -m subjetunes.cli judge   --xml rekordbox.xml --playlist "Folder/My List" --criterion hypnotic \
                            --judge audio_llm:qwen2.5-omni-7b [--only-uncertain]
  python -m subjetunes.cli caption --xml rekordbox.xml --playlist "Folder/My List" --model qwen2.5-omni-7b
  python -m subjetunes.cli benchmark --xml rekordbox.xml --playlist "Folder/My List" --criterion hypnotic \
                            [--audio-model qwen2.5-omni-7b] [--text-llm]
  python -m subjetunes.cli smoke-audio --model qwen2.5-omni-7b --file "C:/Music/track.mp3"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from . import hardware, judging, model_manager, models_registry, pipeline, rbxml, sources
from .cache import Cache
from .config import DATA_DIR, ensure_dirs, load_settings
from .llm import OllamaClient


def _progress(cache: Cache, job_id: str):
    def cb(done: int, total: int, msg: str = "") -> None:
        cache.update_job(job_id, done=done, total=total, message=msg, status="running")
        print(f"[{done}/{total}] {msg}", flush=True)
    return cb


def _setup(args):
    ensure_dirs()
    cfg = load_settings()
    cache = Cache(DATA_DIR / "cache.sqlite")
    job_id = args.job_id or f"{args.cmd}-{uuid.uuid4().hex[:8]}"
    cache.create_job(job_id, args.cmd, vars(args))
    return cfg, cache, job_id


def _tracks(args, cache=None):
    lib = sources.load_library(args.xml)
    ids = pipeline.track_ids_for(lib, args.playlist)
    if not ids:
        raise SystemExit(f"No tracks found for playlists {args.playlist}")
    return lib, ids


def cmd_info(args) -> int:
    cfg = load_settings()
    prof = hardware.profile(cfg.get("hardware"))
    print(json.dumps(prof, indent=2))
    print("\nModel registry:")
    for e in models_registry.eligible(prof):
        print(f"  [{'OK ' if e['fits'] else 'NO '}] {e['id']:<32} {e.get('kind',''):<10} {e['why']}")
    ok, msg = OllamaClient(cfg.get("llm", {}).get("ollama_url", "http://localhost:11434")).health()
    print(f"\nOllama: {'up' if ok else 'down'} - {msg}")
    print(f"llama-server: {model_manager.find_llama_server() or 'not found (run scripts/get_llama_cpp.ps1)'}")
    return 0


def cmd_analyze(args) -> int:
    cfg, cache, job_id = _setup(args)
    lib, ids = _tracks(args)
    tracks = [lib.tracks[t] for t in ids]
    prog = _progress(cache, job_id)
    try:
        res = pipeline.analyze_tracks(tracks, cfg, cache, prog, args.workers)
        if not args.no_clap and cfg.get("clap", {}).get("enabled", True):
            res["clap"] = pipeline.embed_tracks(tracks, cfg, cache, prog)
        cache.update_job(job_id, status="done", message=json.dumps(res))
        print(json.dumps(res, indent=2))
        return 0
    except Exception as e:
        cache.update_job(job_id, status="error", message=f"{e.__class__.__name__}: {e}")
        raise


def _with_audio_server(cfg, model_id: str, fn):
    entry = models_registry.get(model_id)
    if not entry:
        raise SystemExit(f"Unknown model '{model_id}' (see config/models.yaml)")
    prof = hardware.profile(cfg.get("hardware"), detect=False)
    ok, why = models_registry.fits(entry, prof)
    if not ok:
        raise SystemExit(f"{model_id} doesn't fit this PC: {why}")
    ollama = OllamaClient(cfg.get("llm", {}).get("ollama_url", "http://localhost:11434"))
    unloaded = model_manager.free_gpu_for_audio(ollama)
    if unloaded:
        print(f"Unloaded from Ollama to free the GPU: {unloaded}")
    gpu = (prof.get("gpu") or {}).get("device")
    server = model_manager.LlamaServer(entry, port=int(cfg.get("audio_llm", {}).get("port", 8080)), device=gpu)
    print("Starting:", " ".join(server.command()))
    server.start()
    try:
        return fn()
    finally:
        server.stop()


def cmd_judge(args) -> int:
    cfg, cache, job_id = _setup(args)
    lib, ids = _tracks(args)
    c = judging.criterion(cfg, args.criterion)
    if args.only_uncertain:
        ids = judging.uncertain_tracks(cache, c.id, ids)
        print(f"Cascade: {len(ids)} uncertain tracks")
    tracks = [lib.tracks[t] for t in ids]
    prog = _progress(cache, job_id)
    kind, _, model = args.judge.partition(":")

    def run(j):
        res = j.judge(tracks, c, lambda d, t: prog(d, t, f"{j.id} {c.id}"))
        cache.put_judgments(res)
        return len(res)

    try:
        if kind == "audio_llm":
            n = _with_audio_server(cfg, model, lambda: run(judging.audio_llm_judge(cfg, model, cache)))
        elif kind == "text_llm":
            j = judging.text_llm_judge(cfg, lib, ids, cache)
            if model:
                j.model, j.id = model, f"text_llm:{model}"
            n = run(j)
        elif kind in ("probe", "zeroshot"):
            js = [j for j in judging.cheap_judges(cfg, lib, ids, cache, c) if j.kind == kind]
            if not js:
                raise SystemExit(f"{kind} judge not available (embeddings/labels/prompts missing)")
            n = run(js[0])
        else:
            raise SystemExit(f"Unknown judge kind '{kind}'")
        cache.update_job(job_id, status="done", message=f"{n} judgments")
        print(f"{n} judgments stored")
        return 0
    except Exception as e:
        cache.update_job(job_id, status="error", message=f"{e.__class__.__name__}: {e}")
        raise


def cmd_caption(args) -> int:
    cfg, cache, job_id = _setup(args)
    lib, ids = _tracks(args)
    prog = _progress(cache, job_id)
    judge = judging.audio_llm_judge(cfg, args.model, cache)
    have = cache.get_captions()
    todo = [lib.tracks[t] for t in ids if lib.tracks[t].path not in have or args.force]

    def run():
        ok = 0
        for n, t in enumerate(todo):
            try:
                cache.put_caption(t.path, judge.id, judge.version, judge.caption(t))
                ok += 1
            except Exception as e:
                print(f"caption failed for {t.path}: {e}", file=sys.stderr)
            prog(n + 1, len(todo), f"caption {t.name}")
        return ok

    try:
        n = _with_audio_server(cfg, args.model, run)
        cache.update_job(job_id, status="done", message=f"{n} captions")
        return 0
    except Exception as e:
        cache.update_job(job_id, status="error", message=f"{e.__class__.__name__}: {e}")
        raise


def cmd_benchmark(args) -> int:
    """Benchmark judges on the gold set for one criterion. Audio-LLM judges start llama-server."""
    from . import benchmark

    cfg, cache, job_id = _setup(args)
    lib, ids = _tracks(args)
    c = judging.criterion(cfg, args.criterion)
    labels = {t: v for t, v in judging.labels_for(cache, c.id).items() if t in lib.tracks}
    if len(labels) < int(cfg.get("benchmark", {}).get("min_labels", 8)):
        raise SystemExit(f"Only {len(labels)} labels for '{c.id}'. Label more tracks first.")
    model_id = cfg.get("clap", {}).get("model_id", "laion/larger_clap_music")
    emb = pipeline.embeddings_by_track(lib, list(labels), cache, model_id)
    prog = _progress(cache, job_id)
    judges = [j for j in judging.cheap_judges(cfg, lib, list(labels), cache, c) if j.kind != "probe"]
    if args.text_llm:
        judges.append(judging.text_llm_judge(cfg, lib, list(labels), cache))

    def run(extra):
        rows = benchmark.run(c, labels, lib.tracks, judges + extra, emb, cache, lambda d, t: prog(d, t, "benchmark"),
                             probe_id=f"probe:{model_id}")
        print(json.dumps(rows, indent=2))
        return rows

    try:
        if args.audio_model:
            _with_audio_server(cfg, args.audio_model, lambda: run([judging.audio_llm_judge(cfg, args.audio_model, cache)]))
        else:
            run([])
        cache.update_job(job_id, status="done", message="benchmark stored")
        return 0
    except Exception as e:
        cache.update_job(job_id, status="error", message=f"{e.__class__.__name__}: {e}")
        raise


def cmd_smoke_audio(args) -> int:
    """One clip, one question: proves audio input + JSON output work on this llama.cpp build."""
    cfg = load_settings()
    from .models import Criterion, Track

    t = Track(track_id="smoke", location="", path=args.file)
    c = Criterion(id="smoke", name="Vocals present", type="binary", description="Is there a human voice in this excerpt?")

    def run():
        j = judging.audio_llm_judge(cfg, args.model, None)
        t0 = time.time()
        res = j.judge([t], c)
        cap = j.caption(t)
        print(json.dumps({"judgment": [r.__dict__ for r in res], "caption": cap, "seconds": round(time.time() - t0, 1)}, indent=2, ensure_ascii=False))
        return 0 if res else 1

    return _with_audio_server(cfg, args.model, run)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="subjetunes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info")

    def common(p):
        p.add_argument("--playlist", action="append", required=True, help="Full playlist path, repeatable")
        p.add_argument("--job-id")

    p = sub.add_parser("analyze")
    common(p)
    p.add_argument("--no-clap", action="store_true")
    p.add_argument("--workers", type=int)
    p.add_argument("--xml", "--library", dest="xml", required=True,help="rekordbox XML export, master.db, or the rekordbox data folder")

    p = sub.add_parser("judge")
    common(p)
    p.add_argument("--criterion", required=True)
    p.add_argument("--judge", required=True, help="audio_llm:<model id> | text_llm[:<ollama model>] | probe | zeroshot")
    p.add_argument("--only-uncertain", action="store_true")

    p = sub.add_parser("caption")
    common(p)
    p.add_argument("--model", required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("benchmark")
    common(p)
    p.add_argument("--criterion", required=True)
    p.add_argument("--audio-model", help="also benchmark this audio_llm (starts llama-server)")
    p.add_argument("--text-llm", action="store_true", help="also benchmark the Ollama text judge")

    p = sub.add_parser("smoke-audio")
    p.add_argument("--model", required=True)
    p.add_argument("--file", required=True)

    args = ap.parse_args(argv)
    return {"info": cmd_info, "analyze": cmd_analyze, "judge": cmd_judge, "caption": cmd_caption,
            "benchmark": cmd_benchmark, "smoke-audio": cmd_smoke_audio}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
