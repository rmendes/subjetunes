"""Personal probe: a tiny model trained on YOUR labels over cached embeddings.

numpy-only ridge regression (scale) and L2 logistic regression (binary,
one-vs-rest for choice). Trains in milliseconds, so retrain whenever labels change.
"""
from __future__ import annotations

import numpy as np

from ..models import Criterion, Judgment, Track


def _design(X: np.ndarray) -> np.ndarray:
    return np.hstack([X, np.ones((X.shape[0], 1), dtype=X.dtype)])


def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    A = _design(X)
    reg = alpha * np.eye(A.shape[1])
    reg[-1, -1] = 0.0                                   # don't regularise the bias
    return np.linalg.solve(A.T @ A + reg, A.T @ y)


def logistic_fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 25) -> np.ndarray:
    """Newton / IRLS with L2. y in {0,1}. Sample-weighted to balance classes."""
    A = _design(X)
    w = np.zeros(A.shape[1])
    pos = max(1.0, y.sum())
    neg = max(1.0, (1 - y).sum())
    sw = np.where(y > 0.5, 0.5 * len(y) / pos, 0.5 * len(y) / neg)
    reg = l2 * np.eye(A.shape[1])
    reg[-1, -1] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(A @ w, -30, 30)))
        g = A.T @ (sw * (p - y)) + reg @ w
        H = (A * (sw * p * (1 - p))[:, None]).T @ A + reg + 1e-6 * np.eye(A.shape[1])
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class ProbeModel:
    def __init__(self, ctype: str, choices: list[str] | None = None, alpha: float = 1.0):
        self.ctype = ctype
        self.choices = choices or []
        self.alpha = alpha
        self.w = None
        self.ws: dict[str, np.ndarray] = {}

    def fit(self, X: np.ndarray, y: list) -> "ProbeModel":
        if self.ctype == "scale":
            self.w = ridge_fit(X, np.asarray(y, dtype=float), self.alpha)
        elif self.ctype == "binary":
            self.w = logistic_fit(X, np.asarray(y, dtype=float), self.alpha)
        else:
            ya = np.asarray(y, dtype=object)
            self.ws = {c: logistic_fit(X, (ya == c).astype(float), self.alpha) for c in self.choices if (ya == c).any()}
        return self

    def predict(self, X: np.ndarray) -> tuple[list, np.ndarray]:
        A = _design(X)
        if self.ctype == "scale":
            v = np.clip(A @ self.w, 0.0, 1.0)
            return list(v), np.full(len(v), 0.6)
        if self.ctype == "binary":
            p = _sigmoid(A @ self.w)
            return list(p), np.abs(p - 0.5) * 2
        names = list(self.ws)
        P = np.stack([_sigmoid(A @ self.ws[c]) for c in names], axis=1)
        P /= P.sum(axis=1, keepdims=True) + 1e-9
        best = P.argmax(axis=1)
        return [names[i] for i in best], P.max(axis=1)


def cross_val_predict(X: np.ndarray, y: list, ctype: str, choices=None, folds: int = 5, alpha: float = 1.0, seed: int = 0) -> list:
    n = len(y)
    idx = np.random.default_rng(seed).permutation(n)
    preds: list = [None] * n
    for f in range(min(folds, n)):
        test = idx[f::folds]
        train = np.setdiff1d(idx, test)
        if len(train) < 3:
            continue
        m = ProbeModel(ctype, choices, alpha).fit(X[train], [y[i] for i in train])
        vals, _ = m.predict(X[test])
        for i, v in zip(test, vals):
            preds[i] = v
    return preds


class ProbeJudge:
    kind = "probe"

    def __init__(self, embeddings: dict[str, np.ndarray], labels: dict[str, float | str], embed_model_id: str, alpha: float = 1.0):
        self.embeddings = embeddings          # keyed by track_id
        self.labels = labels                  # track_id -> label
        self.alpha = alpha
        self.id = f"probe:{embed_model_id}"
        self.version = f"n{len(labels)}"

    def supports(self, c: Criterion) -> bool:
        have = [t for t in self.labels if t in self.embeddings]
        return len(have) >= 8

    def judge(self, tracks: list[Track], c: Criterion, progress=None) -> list[Judgment]:
        train = [t for t in self.labels if t in self.embeddings]
        X = np.stack([self.embeddings[t] for t in train])
        model = ProbeModel(c.type, c.choices, self.alpha).fit(X, [self.labels[t] for t in train])
        todo = [t for t in tracks if t.track_id in self.embeddings]
        if not todo:
            return []
        vals, conf = model.predict(np.stack([self.embeddings[t.track_id] for t in todo]))
        return [Judgment(t.track_id, c.id, self.id, self.version, v if isinstance(v, str) else float(v), float(cf))
                for t, v, cf in zip(todo, vals, conf)]
