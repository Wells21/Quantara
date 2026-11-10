"""AFML Ch.20 audit (2026-10-31): run Snippets 20.3-20.7 with only the Python 2 -> 3 edits (xrange -> range, print())
and check what they compute.

1. Snippet 20.3 barrierTouch keeps `continue` where the text promises "the earliest barrier touch": it records the LAST
   touching row of each path.
2. Snippet 20.4 builds `parts` from r.shape[0] (1,000 rows) but slices COLUMNS r[:, a:b]: only the first 1,000 of the
   10,000 paths are processed.
3. Snippet 20.7 mpPandasObj refers to `argList`, which is not defined (the argument is pdObj): NameError.
4. Snippets 20.5 linParts / 20.6 nestedParts: partitions checked against the p.308 formulas.
"""
import numpy as np


def barrierTouch(r, width=.5):  # Snippet 20.3 as printed (xrange -> range)
    t, p = {}, np.log((1 + r).cumprod(axis=0))
    for j in range(r.shape[1]):
        for i in range(r.shape[0]):
            if p[i, j] >= width or p[i, j] <= -width:
                t[j] = i
                continue
    return t


def earliest(r, width=.5):
    p = np.log((1 + r).cumprod(axis=0))
    hit = (p >= width) | (p <= -width)
    return {j: int(np.argmax(hit[:, j])) for j in range(r.shape[1]) if hit[:, j].any()}


def linParts(numAtoms, numThreads):  # Snippet 20.5
    parts = np.linspace(0, numAtoms, min(numThreads, numAtoms) + 1)
    return np.ceil(parts).astype(int)


def nestedParts(numAtoms, numThreads, upperTriang=False):  # Snippet 20.6 (xrange -> range)
    parts, numThreads_ = [0], min(numThreads, numAtoms)
    for num in range(numThreads_):
        part = 1 + 4 * (parts[-1] ** 2 + parts[-1] + numAtoms * (numAtoms + 1.) / numThreads_)
        part = (-1 + part ** .5) / 2.
        parts.append(part)
    parts = np.round(parts).astype(int)
    if upperTriang:
        parts = np.cumsum(np.diff(parts)[::-1])
        parts = np.append(np.array([0]), parts)
    return parts


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    r = rng.normal(0, .01, size=(1000, 2000))
    book, first = barrierTouch(r), earliest(r)
    differ = sum(book[j] != first[j] for j in first)
    print(f"1. paths touching: {len(first)} of {r.shape[1]}; book index != earliest touch on {differ} "
          f"({differ / len(first):.1%}); book index is the last touching row: {all(book[j] >= first[j] for j in first)}")
    rows, cols, numThreads = 1000, 10000, 24
    parts = np.ceil(np.linspace(0, rows, min(numThreads, rows) + 1)).astype(int)  # Snippet 20.4 (r.shape[0])
    print(f"2. Snippet 20.4 slices columns {parts[0]}..{parts[-1]} of {cols}: {parts[-1] / cols:.0%} of the paths")
    src = "if linMols:parts=linParts(len(argList[1]),numThreads*mpBatches)"
    print(f"3. Snippet 20.7 signature mpPandasObj(func,pdObj,...); body uses argList -> NameError: {'argList' in src}")
    for n, m in ((20, 6), (100, 24), (1000, 24)):
        lp = linParts(n, m)
        npar = nestedParts(n, m)
        work = [sum(range(a + 1, b + 1)) for a, b in zip(npar[:-1], npar[1:])]  # lower triangle: row i has i tasks
        print(f"4. N={n} M={m}: linParts sizes {sorted(set(np.diff(lp)))}; nestedParts rows {npar.tolist()[:4]}... "
              f"work per molecule min/max {min(work)}/{max(work)} (target {n * (n + 1) / (2 * m):.1f})")
