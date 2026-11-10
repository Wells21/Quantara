import os, sys; sys.path[:0]=[r"C:/Users/Administrator/Desktop/Trading Models/trading_models_v2", r"C:/Users/Administrator/Desktop/Trading Models"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, book_snippets_18_1_18_4 as B
from shared.features import afml_ch18_entropy as Q
print("p.269 example, book code, expanding window:")
for m in ["10000111","10000110","11100001","01100001"]:
    print(" ", m, "book", round(B.konto(m)["h"],4), " port", round(Q.kontoyiannis_entropy(m)["h"],4))
print("matchLength('0101',2,2): book", B.matchLength("0101",2,2), "port", Q.match_length("0101",2,2))
print("matchLength('0111',2,2): book", B.matchLength("0111",2,2), "port", Q.match_length("0111",2,2))
rng=np.random.default_rng(0); diff_ml=diff_k=diff_pmf=0; N=300
for t in range(N):
    m="".join(rng.choice(list("01"), size=int(rng.integers(20,120))))
    n=int(rng.integers(2,len(m)//2+1)); i=int(rng.integers(n,len(m)-n+1))
    diff_ml += B.matchLength(m,i,n)!=Q.match_length(m,i,n)
    w=int(rng.integers(4,40)); diff_k += abs(B.konto(m,w)["h"]-Q.kontoyiannis_entropy(m,w)["h"])>1e-12
    diff_pmf += B.pmf1(m,3)!=Q.pmf1(m,3)
print(f"over {N} random messages: matchLength differs {diff_ml}, konto h differs {diff_k}, pmf1 differs {diff_pmf}")
print("lempelZiv_lib equal:", all(B.lempelZiv_lib(m)==Q.lempel_ziv_lib(m) for m in ["".join(np.random.default_rng(s).choice(list("01"),size=80)) for s in range(50)]))
