"""Repro: the extractor doesn't bind first-person language to the userId.

When you ingest a turn under userId="andrew" with content like "I'm working
on matthammer", the extractor produces triples with bare-string subjects
like "I", "User", or "User's private library" — never "andrew" or any
binding to the userId you actually passed.

`userId` IS honored as a storage-side filter (search and recall scope by
it correctly), but it never reaches the extraction prompt. The extractor
has no way to know who "I" refers to.

Knock-on effect (worse): when a more "concrete" named entity appears in
the same extraction context (e.g. delegated research mentioning a real
person), the extractor can latch onto that entity as the substitute for
first-person references — producing triples like (Shashank, prefers, teal)
when the user said "my favorite color is teal".

Run against a live selfhost. Repeats N times to characterize reliability.

Output expected today: 5/5 runs produce unanchored "User"/"I" subjects.
After a fix that threads userId into the extraction prompt: 0/5 runs.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("CONTEXTO_BASE_URL", "http://localhost:4010")
N_RUNS = 5

USER_TURN = (
    "Earlier I delegated a research task about Shashank, who founded Contexto — "
    "an AI memory platform he started in 2024 from San Francisco. Background: "
    "Shashank previously worked at OpenAI on memory systems. By the way, my favorite "
    "color is teal. I'm also working on a project called matthammer this week — "
    "it's my private library for fast matrix operations, written in Rust. "
    "Just refactored the SIMD path this morning."
)
ASSISTANT_TURN = (
    "Got it. Shashank founded Contexto in 2024 from SF, came from OpenAI memory work. "
    "Noting your favorite color (teal) and the matthammer SIMD refactor."
)


def triples_around(entity: str, agent: str) -> list[dict]:
    r = httpx.get(
        f"{BASE}/v1/graph/triples",
        params={"entity": entity, "agent": agent, "maxResults": 200},
        timeout=15,
    )
    r.raise_for_status()
    return (r.json() or {}).get("triples") or []


def run_one(run_idx: int) -> dict:
    slug = f"repro-{run_idx}-{uuid.uuid4().hex[:8]}"

    httpx.post(f"{BASE}/v1/agents", json={"id": slug, "name": slug}, timeout=15).raise_for_status()
    httpx.post(
        f"{BASE}/v1/ingest",
        json={
            "agent": slug,
            "userId": "andrew",  # <-- passed but not used by the extractor
            "messages": [
                {"role": "user", "content": USER_TURN},
                {"role": "assistant", "content": ASSISTANT_TURN},
            ],
        },
        timeout=300,
    ).raise_for_status()
    time.sleep(2)

    user_facts = triples_around("matthammer", slug) + triples_around("teal", slug)

    misattributed = []   # subject is shashank when it should be the user
    correctly_attributed = []  # subject == "andrew" or contains the userId
    unanchored = []      # subject is "I", "User", "me", "my", or empty

    for t in user_facts:
        subj = str(t.get("subject", "")).strip().lower()
        obj = str(t.get("object", "")).strip().lower()
        if "shashank" in f"{subj} {obj}":
            misattributed.append(t)
        elif "andrew" in subj:
            correctly_attributed.append(t)
        elif subj in ("i", "me", "my", "user", "the user", "") or subj.startswith("user"):
            unanchored.append(t)

    return {
        "slug": slug,
        "user_facts": user_facts,
        "misattributed": misattributed,
        "correctly_attributed": correctly_attributed,
        "unanchored": unanchored,
    }


def main() -> int:
    try:
        httpx.get(f"{BASE}/v1/agents", timeout=2.0).raise_for_status()
    except Exception:
        print(f"selfhost not reachable at {BASE}", file=sys.stderr)
        return 1

    results = []
    for i in range(N_RUNS):
        print(f"\n=== run {i+1}/{N_RUNS} ===")
        try:
            r = run_one(i)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            continue
        results.append(r)
        for t in r["user_facts"]:
            print(f"    ({t.get('subject','?')!r}) -[{t.get('predicate','?')}]-> ({t.get('object','?')!r})")
        print(f"  misattrib={len(r['misattributed'])}  "
              f"correct={len(r['correctly_attributed'])}  "
              f"unanchored={len(r['unanchored'])}")

    total = len(results)
    drifted = sum(1 for r in results if r["misattributed"])
    unanchored_runs = sum(1 for r in results if r["unanchored"] and not r["correctly_attributed"])

    print("\n" + "=" * 60)
    print(f"SUMMARY across {total} runs:")
    print(f"  cross-attributed (user fact → Shashank):       {drifted}/{total}")
    print(f"  unanchored first-person ('User'/'I' subjects): {unanchored_runs}/{total}")
    print(f"  correctly attributed (subject == 'andrew'):    "
          f"{sum(1 for r in results if r['correctly_attributed'])}/{total}")
    print("=" * 60)

    if unanchored_runs == total:
        print("⚠ Reproduced: extractor never binds first-person to userId. RELIABLE.")
        return 0
    elif unanchored_runs >= max(1, total // 2):
        print("⚠ Reproduced intermittently. Still issue-worthy.")
        return 0
    else:
        print("Did not reproduce in this run set.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
