"""AFML Ch.21 audit (2026-10-31): Snippets 21.1-21.7 with only the Python 2 -> 3 edits (xrange -> range, print())
and checks of the chapter's claims.

1. pigeonHole(k, n) yields every ordered composition once: count = C(k+n-1, n-1) (p.321).
2. getAllWeights: 2^N sign vectors per partition (p.323) -> duplicates whenever a weight is 0 (-0 = +0).
3. The "dynamic optimum" of Snippet 21.7 searches only weights on the 1/k grid (k = n by default); Snippet 21.6's static
   solution is continuous, so the "global" optimum is global over the grid only. Count how often static beats dynamic.
4. evalTCosts starts from w_ = 0, i.e. the initial allocation omega* of p.320 is fixed at zero.
"""
import math
from itertools import combinations_with_replacement, product

import numpy as np


def pigeonHole(k, n):  # Snippet 21.1
    for j in combinations_with_replacement(range(n), k):
        r = [0] * n
        for i in j:
            r[i] += 1
        yield r


def getAllWeights(k, n):  # Snippet 21.2
    parts, w = pigeonHole(k, n), None
    for part_ in parts:
        w_ = np.array(part_) / float(k)
        for prod_ in product([-1, 1], repeat=n):
            w_signed_ = (w_ * prod_).reshape(-1, 1)
            if w is None:
                w = w_signed_.copy()
            else:
                w = np.append(w, w_signed_, axis=1)
    return w


def evalTCosts(w, params):  # Snippet 21.3
    tcost = np.zeros(w.shape[1])
    w_ = np.zeros(shape=w.shape[0])
    for i in range(tcost.shape[0]):
        c_ = params[i]['c']
        tcost[i] = (c_ * abs(w[:, i] - w_) ** .5).sum()
        w_ = w[:, i].copy()
    return tcost


def evalSR(params, w, tcost):  # Snippet 21.3
    mean, cov = 0, 0
    for h in range(w.shape[1]):
        params_ = params[h]
        mean += np.dot(w[:, h].T, params_['mean'])[0] - tcost[h]
        cov += np.dot(w[:, h].T, np.dot(params_['cov'], w[:, h]))
    return mean / cov ** .5


def dynOptPort(params, k=None):  # Snippet 21.3
    if k is None:
        k = params[0]['mean'].shape[0]
    n = params[0]['mean'].shape[0]
    w_all, sr = getAllWeights(k, n), None
    for prod_ in product(w_all.T, repeat=len(params)):
        w_ = np.array(prod_).T
        tcost_ = evalTCosts(w_, params)
        sr_ = evalSR(params, w_, tcost_)
        if sr is None or sr < sr_:
            sr, w = sr_, w_.copy()
    return w


def rndMatWithRank(nSamples, nCols, rank, sigma=0, homNoise=True, rng=None):  # Snippet 21.4 (rng passed for seeding)
    rng = rng or np.random.RandomState()
    U, _, _ = np.linalg.svd(rng.randn(nCols, nCols))
    x = np.dot(rng.randn(nSamples, rank), U[:, :rank].T)
    if homNoise:
        x += sigma * rng.randn(nSamples, nCols)
    else:
        sigmas = sigma * (rng.rand(nCols) + .5)
        x += rng.randn(nSamples, nCols) * sigmas
    return x


def statOptPortf(cov, a):  # Snippet 21.6
    cov_inv = np.linalg.inv(cov)
    w = np.dot(cov_inv, a)
    w /= np.dot(np.dot(a.T, cov_inv), a)
    w /= abs(w).sum()
    return w


def example(seed, size=3, horizon=2):  # Snippets 21.5-21.7
    rng = np.random.RandomState(seed)
    params = []
    for h in range(horizon):
        x = rndMatWithRank(1000, size, size, 0., rng=rng)
        mean_, cov_ = rng.normal(size=(size, 1)), np.cov(x, rowvar=False)
        c_ = rng.uniform(size=cov_.shape[0]) * np.diag(cov_) ** .5
        params.append({'mean': mean_, 'cov': cov_, 'c': c_})
    w_stat = np.hstack([statOptPortf(p['cov'], p['mean']) for p in params])
    sr_stat = evalSR(params, w_stat, evalTCosts(w_stat, params))
    w_dyn = dynOptPort(params)
    sr_dyn = evalSR(params, w_dyn, evalTCosts(w_dyn, params))
    return sr_stat, sr_dyn


if __name__ == "__main__":
    for k, n in ((6, 3), (3, 3), (10, 4)):
        got = list(pigeonHole(k, n))
        print(f"1. pigeonHole({k},{n}): {len(got)} compositions, C(k+n-1,n-1) = {math.comb(k + n - 1, n - 1)}, "
              f"distinct {len({tuple(g) for g in got})}, (1,2,3) and (3,2,1) both present: "
              f"{[1, 2, 3] in got and [3, 2, 1] in got}" if (k, n) == (6, 3) else
              f"1. pigeonHole({k},{n}): {len(got)} compositions, C(k+n-1,n-1) = {math.comb(k + n - 1, n - 1)}")
    for k, n in ((3, 3), (4, 4)):
        w = getAllWeights(k, n)
        distinct = len({tuple(np.round(c, 12) + 0.0) for c in w.T})
        print(f"2. getAllWeights({k},{n}): {w.shape[1]} columns, {distinct} distinct ({w.shape[1] - distinct} duplicates); "
              f"all |w| sum to 1: {np.allclose(abs(w).sum(axis=0), 1)}")
    res = [example(s) for s in range(200)]
    stat_better = sum(st > dy + 1e-12 for st, dy in res)
    print(f"3. size 3, horizon 2, 200 seeds: static SR > 'dynamic optimum' SR in {stat_better} of 200 "
          f"(median static {np.median([r[0] for r in res]):.3f}, dynamic {np.median([r[1] for r in res]):.3f})")
    w = getAllWeights(3, 3)
    print(f"4. trajectories searched for size 3, horizon 2: {w.shape[1] ** 2:,}; size 4: {getAllWeights(4, 4).shape[1] ** 2:,}")
