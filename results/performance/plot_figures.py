#!/usr/bin/env python3
"""Rebuild the two publication figures from adjacent, provenance-bearing JSON."""
from __future__ import annotations
import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/opensearch-figures-mpl")
import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "opensearch-task-header-figures-v1"
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "figures"
BLUE = "#2E6F9E"
BLUE_LIGHT = "#A9C5D8"
GOLD = "#C48723"
INK = "#263746"
MUTED = "#566774"
GRID = "#D9E0E5"
PALE = "#F3F6F8"
WHITE = "#FFFFFF"


def load(name):
    return json.loads((HERE / name).read_text())


def style_axis(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9, length=0, pad=5)
    ax.grid(axis="y", color=GRID, linewidth=.7, zorder=0)


def figure_mechanism():
    d = load("mechanism-data.json")
    fig = plt.figure(figsize=(12.5, 6.35), facecolor=WHITE)
    gs = fig.add_gridspec(2, 2, width_ratios=[1, 1.12], height_ratios=[.92, 1.08],
                          wspace=.32, hspace=.46, left=.075, right=.975, top=.85, bottom=.19)
    fig.suptitle("Scroll response-header history and cleanup evidence", x=.075, y=.96,
                 ha="left", fontsize=15, fontweight="bold", color=INK)
    fig.text(.075, .895, "Source-supported carry-forward path · observed stock counts · separately instrumented diagnostic",
             ha="left", fontsize=9.5, color=MUTED)

    # Source-supported pathway and proposed intervention. The plotted diagnostic below
    # measures the list length at doStartNextScroll entry; it does not instrument capture.
    flow = fig.add_subplot(gs[0, :])
    flow.set_xlim(0, 100); flow.set_ylim(0, 10); flow.axis("off")
    flow.text(.01, .98, "MECHANISM / INTERVENTION", transform=flow.transAxes, ha="left", va="top",
              fontsize=9, fontweight="bold", color=MUTED)
    def box(ax, x0, y0, w, h, text, fc, ec, fs=8.5, color=INK, weight="normal"):
        p = FancyBboxPatch((x0,y0),w,h,boxstyle="round,pad=.18,rounding_size=.35",
                           facecolor=fc,edgecolor=ec,linewidth=1.25)
        ax.add_patch(p)
        ax.text(x0+w/2,y0+h/2,text,ha="center",va="center",fontsize=fs,color=color,
                fontweight=weight,wrap=True)
    def arrow(ax, x1,y1,x2,y2,color=MUTED,style="-|>",lw=1.3,connection="arc3",linestyle="-"):
        ax.add_patch(FancyArrowPatch((x1,y1),(x2,y2),arrowstyle=style,mutation_scale=11,
                                     linewidth=lw,color=color,connectionstyle=connection,linestyle=linestyle))
    box(flow,2,5.35,20,2.25,"TaskResourceTrackingService\nlocally replaces the current\nTASK_RESOURCE_USAGE value",PALE,GRID,8.3,INK,"bold")
    box(flow,28,5.35,21,2.25,"Saved ThreadContext restore\nmerges prior response-header\nvalues into active context",PALE,GRID,8.3,INK,"bold")
    box(flow,55,5.35,18,2.25,"Candidate patch\nremoves only\nTASK_RESOURCE_USAGE", "#FFF7E8", GOLD,8.3,INK,"bold")
    box(flow,79,5.35,19,2.25,"Next-scroll path reads\ncontext list at\ndoStartNextScroll", "#EDF4F8",BLUE_LIGHT,8.3,INK,"bold")
    arrow(flow,22.3,6.48,27.5,6.48)
    arrow(flow,49.3,6.48,54.5,6.48,GOLD)
    arrow(flow,73.3,6.48,78.5,6.48,GOLD)
    # Baseline route skips the proposed cleanup wrapper.
    arrow(flow,39,4.85,86,4.85,BLUE,style="-|>",lw=1.35,linestyle="--")
    flow.text(62.5,4.35,"Baseline path bypasses the proposed cleanup",ha="center",va="center",
              fontsize=8.1,color=BLUE)
    flow.text(62.5,3.35,"Repeated rounds can grow the retained list; the diagnostic reads list length at the next-scroll call.",
              ha="center",va="center",fontsize=8.2,color=MUTED)
    flow.text(62.5,2.2,"Candidate cleanup runs immediately before terminal listener delegation; other response-header keys remain.",
              ha="center",va="center",fontsize=8.2,color=MUTED)

    # Exact raw stock response-header counts.
    ax = fig.add_subplot(gs[1,0])
    obs=d["stock_observation"]["observations"]
    vals=[r["count"] for r in obs]
    labels=["Search","UBQ 16","UBQ 1\nfirst","UBQ 1\nrepeat"]
    bars=ax.bar(range(4),vals,color=[BLUE_LIGHT,BLUE_LIGHT,BLUE,BLUE],width=.65,zorder=3)
    ax.set_title("Observed in stock · raw response-header lines",loc="left",fontsize=10.3,fontweight="bold",color=INK,pad=9)
    ax.set_ylabel("Count",fontsize=8.5,color=MUTED)
    ax.set_xticks(range(4),labels)
    ax.set_ylim(0,145); ax.set_yticks([0,32,64,96,128])
    ax.grid(axis="y",color=GRID,linewidth=.7,zorder=0)
    for bar,val in zip(bars,vals): ax.text(bar.get_x()+bar.get_width()/2,val+3,str(val),ha="center",va="bottom",fontsize=9,fontweight="bold",color=INK)
    style_axis(ax)
    ax.text(.01,-.34,"128 documents updated per UBQ. Counts are header lines, not bytes or full resource totals.",
            transform=ax.transAxes,fontsize=7.8,color=MUTED,va="top")

    # Directly instrumented measurement at doStartNextScroll entry.
    ax=fig.add_subplot(gs[1,1])
    arms=d["instrumented_diagnostic"]["arms"]
    n=arms["baseline"]["n"]
    bs=arms["baseline"]["sequence"]
    cs=arms["cleanup_candidate"]["sequence"]
    baseline=[bs["start"]+i*bs["step"] for i in range(n)]
    candidate=[cs["constant"]]*n
    x=list(range(1,n+1))
    ax.plot(x,baseline,color=BLUE,linewidth=2,label="Baseline",zorder=3)
    ax.scatter([1,32,64,96,128],[baseline[i-1] for i in [1,32,64,96,128]],s=16,color=BLUE,zorder=4)
    ax.plot(x,candidate,color=GOLD,linewidth=2,linestyle=(0,(4,2)),label="Cleanup candidate",zorder=3)
    ax.scatter([1,32,64,96,128],[0]*5,s=18,facecolor=WHITE,edgecolor=GOLD,linewidth=1.3,zorder=4)
    ax.set_title("Instrumented diagnostic · entries at next-scroll call",loc="left",fontsize=10.3,fontweight="bold",color=INK,pad=9)
    ax.set_xlabel("doStartNextScroll call (of 128)",fontsize=8.5,color=MUTED)
    ax.set_ylabel("Context-list entries",fontsize=8.5,color=MUTED)
    ax.set_xlim(0,132);ax.set_ylim(-5,137);ax.set_xticks([1,32,64,96,128]);ax.set_yticks([0,32,64,96,128])
    ax.grid(color=GRID,linewidth=.7,zorder=0);ax.legend(frameon=False,fontsize=7.8,loc="upper left",ncol=2)
    style_axis(ax)
    ax.text(.01,-.34,"Baseline 0…127; candidate 0 at all 128 reads. Separate instrumented run; no byte or latency measure.",
            transform=ax.transAxes,fontsize=7.8,color=MUTED,va="top")
    fig.text(.075,.045,"Mechanism description is source-supported; the diagnostic records context-list length immediately before client.searchScroll.",fontsize=8.2,color=MUTED)
    for ext in ("svg","png"):
        metadata={"Title":d["title"],"Description":"Source-supported scroll header carry-forward mechanism, proposed one-key cleanup, raw stock header counts and measured next-scroll context-list lengths."}
        metadata.update({"Creator":"Reproducible local plotting script","Date":None} if ext=="svg" else {"Software":"Matplotlib"})
        fig.savefig(OUT/f"accumulation.{ext}",dpi=180,bbox_inches="tight",
                    metadata=metadata)
    plt.close(fig)

def figure_latency():
    d=load("latency-data.json")
    fig,ax=plt.subplots(figsize=(8.45,4.65),facecolor=WHITE)
    fig.subplots_adjust(left=.245,right=.98,top=.78,bottom=.31)
    fig.suptitle("Cleanup reduced late-search p99 in this experiment",x=.245,y=.95,
                 ha="left",fontsize=13.3,fontweight="bold",color=INK)
    fig.text(.245,.875,"Common pacing in both arms · one A–B–B–A sequence",ha="left",fontsize=9,color=MUTED)
    pairs=d["pairs"]
    ys=[1,0]
    for pair,y in zip(pairs,ys):
        b=pair["baseline_p99_ms"];c=pair["cleanup_p99_ms"]
        blo=b["lower"];clo=c["lower"];chi=c["upper"]
        ax.plot([blo,clo],[y,y],color=GRID,linewidth=2.0,zorder=1)
        ax.errorbar(blo,y,xerr=[[0],[b["upper"]-blo]],fmt="o",markersize=7.5,
                    color=BLUE,ecolor=BLUE,capsize=3,elinewidth=1.8,zorder=4)
        ax.errorbar(clo,y,xerr=[[0],[chi-clo]],fmt="o",markersize=7.5,
                    color=GOLD,ecolor=GOLD,capsize=3,elinewidth=1.8,zorder=4)
        delta=pair["absolute_reduction_ms"]
        if delta["lower"]==delta["upper"]:
            reduction=f"Reduction {delta['lower']:.2f} ms · {pair['reduction_percent']['lower']:.1f}% lower"
        else:
            reduction=f"Reduction {delta['lower']:.2f}–{delta['upper']:.2f} ms · ≥{pair['reduction_percent']['lower']:.1f}% lower"
        ax.text(1,y+.24,reduction,ha="left",va="bottom",fontsize=8.7,color=INK,fontweight="bold")
        ax.text(blo,y-.17,f"Baseline {pair['baseline_arm']} · {blo:.2f}",ha="center",va="top",fontsize=7.7,color=BLUE)
        ctext=f"Cleanup {pair['cleanup_arm']} · {clo:.2f}" if clo==chi else f"Cleanup {pair['cleanup_arm']} · {clo:.2f}–{chi:.2f}"
        ax.text((clo+chi)/2,y-.17,ctext,ha="center",va="top",fontsize=7.7,color="#9A6718")
    ax.set_yticks(ys,["A1 baseline / B1 cleanup","A2 baseline / B2 cleanup"])
    ax.set_xlim(0,36);ax.set_ylim(-.50,1.48)
    ax.set_xlabel("Late-window nearest-rank p99 (ms)",fontsize=9,color=MUTED,labelpad=7)
    ax.set_xticks([0,5,10,15,20,25,30,35]);ax.grid(axis="x",color=GRID,linewidth=.65);ax.grid(axis="y",visible=False)
    for side in ("top","right","left"):ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(axis="y",length=0,labelsize=8.3,colors=INK,pad=7)
    ax.tick_params(axis="x",length=0,labelsize=8,colors=MUTED,pad=5)
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([0],[0],marker="o",color="none",markerfacecolor=BLUE,markeredgecolor=BLUE,label="Baseline",markersize=6),
                       Line2D([0],[0],marker="o",color="none",markerfacecolor=GOLD,markeredgecolor=GOLD,label="Cleanup candidate",markersize=6)],
              frameon=False,loc="upper right",ncol=2,fontsize=7.5)
    fig.text(.245,.195,"B2 late probes: 2,500 / 2,501 · p99 bound 9.39–9.59 ms (not a confidence interval).",fontsize=8.1,color=MUTED)
    fig.text(.245,.115,"Late window: 150k–180k acknowledged updates · tracking on · not an unmodified-stock baseline or population estimate.",fontsize=8.1,color=MUTED)
    for ext in ("svg","png"):
        metadata={"Title":d["title"],"Description":"Late search p99 paired comparison for a single common-paced ABBA experiment; B2 has a missing-probe bound, not a confidence interval."}
        metadata.update({"Creator":"Reproducible local plotting script","Date":None} if ext=="svg" else {"Software":"Matplotlib"})
        fig.savefig(OUT/f"late-search-latency.{ext}",dpi=150,bbox_inches="tight",
                    metadata=metadata)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    figure_mechanism()
    figure_latency()
    print("Wrote accumulation.{svg,png} and late-search-latency.{svg,png}")
