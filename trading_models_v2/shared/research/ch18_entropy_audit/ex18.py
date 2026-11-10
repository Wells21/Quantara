import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, book_snippets_18_1_18_4 as B
rng=np.random.default_rng(0); x=rng.normal(size=1000)
def q8(v): return "".join(map(str,np.searchsorted(np.quantile(v,np.linspace(0,1,9)[1:-1]),v)))
for name,v in [("18.3 iid N(0,1)",x),("18.4 AR(1) rho=.5",None)]:
    if v is None:
        y=np.zeros(1000); 
        for t in range(1000): y[t]=(0.5*y[t-1] if t else 0)+x[t]
        v=y
    m=q8(v); print(name, "plug-in w=1..3:", [round(B.plugIn(m,w)[0],3) for w in (1,2,3)],
        "konto w10:", round(B.konto(m,10)["h"],3), "w100:", round(B.konto(m,100)["h"],3), "| true: 8-letter quantised iid = 3 bits")
th=np.full(10,.1); print("18.5a H =", 1-np.exp(-(th*np.log(th)).sum())/10)
print("18.5b sum theta =", sum(1-i/55 for i in range(1,11)), "(not 1)")
