import json
R = json.load(open("results.json")); M = R["meta"]; S = R["results"]
def bw(t):
    if not t: return "–"
    return f"{t['name'].split(' /')[0]} {t['pnl_usd']:+.2f} ({t['pnl_pct']:+.0f}%, {t['reason']})"
f = lambda v, fmt: "–" if v is None else fmt.format(v)
def row(k, title):
    s = S[k]
    return (f"| {title} | ${s['start']:,.0f} → ${s['end']:,.2f} | {s['pnl']:+.2f} ({s['pnl_pct']:+.1f}%) | {s['n']} | "
            f"{f(s['win_rate'], '{:.0f}%')} | {f(s['avg_win'], '{:+.2f}')} / {f(s['avg_loss'], '{:+.2f}')} | {s['mdd_pct']:.1f}% | "
            f"{s['dex_fees']+s['net_fees']+s['slippage']:.2f} ({s['dex_fees']:.2f} / {s['net_fees']:.2f} / {s['slippage']:.2f}) | {s['gross_price_pnl']:+.2f} | {bw(s['best'])} | {bw(s['worst'])} |")
hdr = ("| Run | Start → End | Net P&L $ (%) | Trades | Win rate | Avg win / avg loss $ | Max DD | Total costs $ (DEX fee / network fee / slippage) | P&L before costs $ | Best trade | Worst trade |\n"
       "|---|---|---|---|---|---|---|---|---|---|---|")
def table(book):
    out = [hdr]
    for nm, t in (("desk", "Desk (gated)"), ("baseline", "Baseline (no gates)")):
        for per, pt in (("full", "full window"), ("dev", "dev (first 70%)"), ("heldout", "held-out (last 30%)")):
            out.append(row(f"{nm}_{book}_{per}", f"{t} – {pt}"))
    return "\n".join(out)
open("_table150.md", "w").write(table(150)); open("_table1000.md", "w").write(table(1000))
print(table(150)); print(); print(table(1000))
for k, s in S.items(): print(k, s["reasons"])
