# The gallery hub: off GitHub Pages, one gallery for every node

The gallery has outgrown GitHub Pages, and a second node has shown up that cannot publish to it.
This plan moves the public gallery to Cloudflare — the Worker and D1 that already carry votes,
likes, views, prompts and critiques, plus R2 for files — on `sketchgen.davidcarroll.org`, and
turns each generator node into a spoke that publishes to it. It is four steps. Step 0 is urgent
and needs none of the rest.

Why now, in numbers read on 2026-09-22:

- **The published tree is 1.2 GB, and 1,166 MB of it is PNG.** Per entry, `strip.png` is about
  2 MB (859 MB in all), `gate.png` about 1.2 MB (264 MB), and the 22 `ghost.png` 43 MB. The HTML
  is 35 MB. GitHub Pages caps a published site at 1 GB; the repository reports 1.16 GB and
  1,976 commits, and each Pages build takes three to four minutes.
- **No page shows `gate.png`.** It is copied into every entry directory and nothing on the
  gallery refers to it.
- **`index.html` is 3.2 MB and `kiosk.json` 3.3 MB.** Every visitor downloads the whole gallery.
- **Every judgment makes every page stale.** Entry pages bake in what changes — standing bars,
  the compass, critique chips, the lineage ledger's children — so `publish-index` re-renders all
  of them, about 25 minutes, and pushes the result.
- **A second node cannot publish.** `d12-node-profcarroll` (RTX 5070 Ti) made 101 jobs in 2 h 20 m
  on 2026-09-22. Its entry 1 collides with sld-cloud's entry 1; its judgments live in its own
  database, so no pair ever crosses nodes; the publisher is a `git push` from one locked
  checkout. Its drop-in says `Private node: no publishing`, correctly.

Repositories and conventions as in `held-batch.md`: `profcarroll/sketchgen`, Python 3.12, stdlib
only, `python3 -m unittest discover -s tests`, no network in a test. Small PRs, one packet per
branch.

## 0. What the second node is for

The d12 run is the reason to want one gallery, so its finding belongs here. It used the same 100
prompts as the harvest of sld-cloud's top-scored entries (`harvest-2026-09-22.json`):

| same 100 prompts | d12 (GPU) | sld-cloud originals |
|---|---|---|
| median attempt wall time | 12.9 s | 97.8 s |
| decode / prefill, tok/s | 99 / 6,849 | 22 / 896 |
| first-attempt pass | 67% | 71% |
| median `sketch.js` | 104 lines | 90.5 lines |

Both nodes run the same weights (`qwen3-coder` digest `06c1097efce0`, `gemma4:e4b` `c6eb396dbd59`).
What differs is everything around them: d12 ran `executor-v3`, harness 4, treatment rules and
`num_ctx 16384` on all 100; the originals are 30 v1, 52 v2, 18 v3, 55/45 treatment/control, ten
by other models, `num_ctx` unrecorded; and the originals are the *winners*, which any remake
regresses from. The difference a person sees is real and is not yet the GPU's.

Measuring it needs (a) the same 100 prompts re-run on sld-cloud under d12's settings, so the
hardware is the only variable left, and (b) both nodes' entries in **one pool of blind pairs**,
scored by the Bradley–Terry machinery the gallery already has. (b) is Step 3.

## 1. Decisions

### 1.1 Cloudflare is the hub; nodes are spokes

The alternatives, and why not:

- **A Pages repository per node, and an aggregate index.** Pairs still never cross nodes, and
  the size problem doubles.
- **Several nodes pushing to one repository.** Push races, colliding ids, the same 1 GB.
- **A web server serving pages from the database (PHP, or Python).** No PHP is needed — the
  renderer is Python and `web.py` already serves pages from the database. The question is where
  it runs. A lab box on the university network is not a public host. sld-cloud could be, but then
  public traffic lands on the machine that runs model-written code, and the gallery is down
  whenever the node is. And with two nodes, both would have to ship rows to it: it would be the
  hub, with worse availability.

The Worker and D1 are already the dynamic half of the gallery, already hold the GitHub sign-in,
and already sync with the node (`/pull`). R2 serves files with no egress charge and speaks the
S3 API; D1 is SQLite. Leaving later is an export, not a rewrite. Cost: Workers paid ($5/mo);
R2 at a few GB is cents.

### 1.2 An entry is two things: what never changes, and what does

- **Immutable**, written once at publish: the sketch, its `index.html`, the strip and ghost
  frames, the statement, provenance (models, prompt versions, attempts, gate timings). Rendered
  by the Python renderer, as now, and uploaded to R2.
- **Mutable**, in D1: scores and standings, critiques, lineage children, counts, state. The
  page asks the Worker for `/e/<id>.json` and paints it, as it already paints counts.

That is what ends the re-render: a judgment changes a row, not a page. A template change is
still a re-render, but of immutable pages, by choice and not because a number moved.

### 1.3 Publishing is an upload, not a push

A node publishes with an authenticated `PUT` to the Worker — a bearer token per node, the
`PULL_TOKEN` pattern — carrying the rendered files and the entry's row. No git, no flock, no
`rejected — fetch first`. The Python side stays stdlib (`urllib.request`). The operator still
chooses what is published; the Held page's button calls the upload instead of `publish`.

### 1.4 Ids carry the node

A published entry is `<node>:<local id>`. sld-cloud's are shown bare, so the 1,049 existing
URLs do not move; any other node's carry its prefix (`/e/d12-17/`). A node's name is
registered on the hub with its token, the way `paid models add` registers a model id.

### 1.5 The hub hands out pairs

Pairs are drawn from the whole pool by the hub, so a d12 entry meets a sld-cloud entry blind. A
node's idle judge asks the hub for a pair, fetches the two strips from R2, judges, and posts the
verdict. `judge.py:assert_blind()` is unchanged: what the judge sees is still A and B.

### 1.6 Sketches live on a different site from the gallery

Every `*.davidcarroll.org` host is one *site*: `SameSite=Lax` does not separate subdomains, and
the operator's personal site lives on that domain. So:

- **`sketchgen.davidcarroll.org`** — gallery pages and the Worker's API on one host. The session
  cookie becomes first-party, and the bearer-token-in-storage workaround the Worker carries for
  a cross-site gallery (`writepath/worker.js`, `sessionToken`) can retire.
- **Model-written sketches** — never under `davidcarroll.org`. Served from R2 through a Worker
  on a `workers.dev` hostname, which is on the Public Suffix List and so its own site, exactly
  the property github.io gives today. A throwaway second domain is the alternative if the URL
  matters.

### 1.7 GitHub keeps the code and becomes the archive

`sketchgen-gallery` is frozen at the switch. Each `e/<id>/index.html` becomes a stub that
redirects to the new address (meta refresh plus `rel=canonical`; Pages has no server
redirects), so printed QR codes and shared links keep working.

### 1.8 The Worker is deployed by the deploy, not by hand

It has drifted behind `main` before (2026-09-19). Once it serves the gallery, drift is an
outage. Step 2 puts `wrangler deploy` into a deploy script with a version endpoint to check.

## 2. The steps

### Step 0 — fit on Pages (now)

- **Publish web copies of the frames.** The gate's PNGs stay on the node, byte-for-byte, as the
  judge's and critic's evidence and the pair hash's input. What the gallery gets is a WebP beside
  each (`strip.webp`, `ghost.webp`), encoded by the same Chromium the gate runs, cached next to
  the PNG. On four real frames the copy came to 38–340 KB against 1.2–3.2 MB, about 200 KB a
  strip on average.
- **Stop publishing `gate.png`.** Nothing shows it.
- **A backfill verb** encodes the ~1,200 existing entries once, so the next `publish-index`
  replaces every PNG in the tree.
- **Paginate the index**, so a visit is not 3.2 MB.
- **Re-run the harvest on sld-cloud** under d12's settings (§0).

Expected: the tree from ~1.2 GB to about 300 MB, most of it the WebP strips. The repository's history keeps the old PNGs;
squashing it is a separate, destructive choice (§4).

### Step 1 — the mutable half moves to D1

The node pushes scores, standings, critiques and lineage to D1 on the sync it already runs.
Entry pages lose the baked-in values and paint them from `/e/<id>.json`. `publish-index` stops
re-rendering entries. Still hosted on Pages. Worth doing under every option in §1.1.

### Step 2 — files move to R2, on `sketchgen.davidcarroll.org`

R2 bucket, the gallery Worker route on the custom domain, the sketch host on `workers.dev`
(§1.6), publish-by-upload (§1.3), the deploy script (§1.8), redirect stubs (§1.7).

### Step 3 — a second node publishes

Node registration and tokens, prefixed ids (§1.4), hub-issued pairs (§1.5). The d12 entries go
into the pool, and the A/B in §0 starts measuring.

## 3. Acceptance

1. Step 0: the gallery tree under 400 MB; no `gate.png` in it; every entry page and the kiosk
   show their frames; the judge and critic still read PNGs on the node.
2. Step 1: a judgment appears on its entry page with no render and no push.
3. Step 2: an entry published on sld-cloud appears at `sketchgen.davidcarroll.org/e/<id>/`; its
   sketch runs from the `workers.dev` host; an old github.io URL lands on it.
4. Step 3: a d12 entry is published as `d12-<id>`; a verdict exists whose pair has one entry
   from each node.

## 4. Out of scope, and open

- **Squashing the gallery repository's history.** It would bring the repository itself under
  1 GB, but it is a force-push to a public repository and every clone breaks. Only if GitHub
  complains about the repository rather than the site.
- **Serving thumbnails.** Strips are 5,132 px wide and shown far smaller; a half-size copy would
  cut the frames again by about three. After Step 0 has shown what the format alone does.
- **Moving the operator console off the tunnel.** A different auth decision
  (`models-console.md` §6, the billing plan).
- **Where the Python renderer ends.** If the immutable page ever needs data only the hub has,
  rendering moves into the Worker. Not before.
