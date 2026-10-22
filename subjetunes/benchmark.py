"""Judge benchmarking against the user's gold-set labels, auto-pick, and label suggestions."""
from __future__ import annotations

import time
import uuid
from typing import Any

import numpy as np

from .judges.probe import cross_val_predict
from .models import Criterion, Judgment, Track


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x), dtype=float)
    r[order] = np.arange(len(x), dtype=float)
    # average ties
    vals, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=r)
    return (sums / counts)[inv]


def spearman(a, b) -> float:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 3 or np.all(a == a[0]) or np.all(b == b[0]):
        return float("nan")
    ra, rb = _rank(a), _rank(b)
    return float(np.corrcoef(ra, rb)[0, 1])


def macro_f1(y_true: list, y_pred: list) -> float:
    classes = sorted(set(y_true) | set(y_pred), key=str)
    f1s = []
    for c in classes:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return float(np.mean(f1s)) if f1s else float("nan")


def agreement(ctype: str, labels: list, preds: list) -> tuple[str, float]:
    pairs = [(l, p) for l, p in zip(labels, preds) if p is not None]
    if not pairs:
        return ("n/a", float("nan"))
    l, p = zip(*pairs)
    if ctype == "scale":
        return ("spearman", spearman(l, p))
    if ctype == "binary":
        return ("macro_f1", macro_f1([int(round(float(x))) for x in l], [int(float(x) >= 0.5) for x in p]))
    return ("macro_f1", macro_f1(list(l), list(p)))


def stability(ctype: str, run1: dict[str, Any], run2: dict[str, Any]) -> float:
    """Mean abs difference (scale/binary) or disagreement rate (choice) between two runs. Lower is better."""
    common = [t for t in run1 if t in run2]
    if not common:
        return float("nan")
    if ctype == "choice":
        return float(np.mean([run1[t] != run2[t] for t in common]))
    return float(np.mean([abs(float(run1[t]) - float(run2[t])) for t in common]))


def auto_pick(rows: list[dict[str, Any]], n_tracks: int, budget_s: float, min_gain: float = 0.02) -> str | None:
    """Highest agreement among judges whose projected runtime fits the budget; ties -> cheaper."""
    ok = [r for r in rows if r.get("agreement") == r.get("agreement")  # not NaN
          and (r.get("sec_per_track", 0) * n_tracks) <= budget_s]
    if not ok:
        return None
    ok.sort(key=lambda r: (-r["agreement"], r.get("sec_per_track", 0)))
    best = ok[0]
    # prefer a cheaper judge if it's within min_gain of the best
    cheaper = [r for r in ok if r["agreement"] >= best["agreement"] - min_gain]
    cheaper.sort(key=lambda r: r.get("sec_per_track", 0))
    return cheaper[0]["judge_id"]


def suggest_to_label(embeddings: dict[str, np.ndarray], labeled: set[str], k: int = 10, seed: int = 0) -> list[str]:
    """Farthest-point sampling: tracks most unlike anything already labeled."""
    ids = [t for t in embeddings if t not in labeled]
    if not ids:
        return []
    X = np.stack([embeddings[t] for t in ids])
    if labeled & set(embeddings):
        L = np.stack([embeddings[t] for t in labeled if t in embeddings])
        d = np.min(1 - X @ L.T, axis=1)
    else:
        d = np.full(len(ids), np.inf)
        d[np.random.default_rng(seed).integers(len(ids))] = -1     # random start
    chosen: list[int] = []
    for _ in range(min(k, len(ids))):
        i = int(np.argmax(d))
        chosen.append(i)
        d = np.minimum(d, 1 - X @ X[i])
        d[chosen] = -np.inf
    return [ids[i] for i in chosen]


def run(
    criterion: Criterion,
    labels: dict[str, Any],
    tracks: dict[str, Track],
    judges: list,
    embeddings: dict[str, np.ndarray] | None = None,
    cache=None,
    progress=None,
    probe_id: str = "probe",
) -> list[dict[str, Any]]:
    """Evaluate every judge on the labeled tracks. Probes use 5-fold cross-validation."""
    lab_ids = [t for t in labels if t in tracks]
    rows: list[dict[str, Any]] = []
    run_id = uuid.uuid4().hex[:8]

    if embeddings:
        have = [t for t in lab_ids if t in embeddings]
        if len(have) >= 8:
            t0 = time.time()
            X = np.stack([embeddings[t] for t in have])
            preds = cross_val_predict(X, [labels[t] for t in have], criterion.type, criterion.choices)
            metric, val = agreement(criterion.type, [labels[t] for t in have], preds)
            rows.append({"judge_id": probe_id, "metric": metric, "agreement": val, "n": len(have),
                         "sec_per_track": (time.time() - t0) / len(have), "stability": 0.0})

    for n, j in enumerate(judges):
        if not j.supports(criterion):
            continue
        t0 = time.time()
        res: list[Judgment] = j.judge([tracks[t] for t in lab_ids], criterion)
        dt = time.time() - t0
        got = {r.track_id: r.value for r in res}
        metric, val = agreement(criterion.type, [labels[t] for t in lab_ids], [got.get(t) for t in lab_ids])
        rows.append({"judge_id": j.id, "metric": metric, "agreement": val, "n": len(got),
                     "sec_per_track": dt / max(1, len(lab_ids)), "stability": float("nan")})
        if cache is not None:
            cache.put_judgments(res)
        if progress:
            progress(n + 1, len(judges))

    if cache is not None:
        cache.put_benchmark(run_id, [(criterion.id, r["judge_id"], r["metric"], r["agreement"], r["n"]) for r in rows])
    return rows
