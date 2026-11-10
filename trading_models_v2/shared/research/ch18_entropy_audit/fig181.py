import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, book_snippets_18_1_18_4 as B
rng=np.random.default_rng(0)
for L in (10,7,5,2):
    hs={"expanding":[], "window10":[], "window25":[]}
    for _ in range(300):
        x=rng.normal(size=100)
        q=np.quantile(x,np.linspace(0,1,L+1)[1:-1]); m="".join(map(str,np.searchsorted(q,x)))
        hs["expanding"].append(B.konto(m)["h"]); hs["window10"].append(B.konto(m,10)["h"]); hs["window25"].append(B.konto(m,25)["h"])
    print(L, {k: (round(np.mean(v),3), round(np.std(v),3)) for k,v in hs.items()}, "true quantized entropy log2(L)=",round(np.log2(L),3))
print("Gaussian H nats", 0.5*np.log(2*np.pi*np.e), "bits", 0.5*np.log2(2*np.pi*np.e))
