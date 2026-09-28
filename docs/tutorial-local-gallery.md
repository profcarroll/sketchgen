# Tutorial: the local gallery

A walk through the publishing pieces built on 2026-09-28, in the order you would try them. Each
exercise is one session's work and ends in something you can see. The reasons are in
`docs/plans/local-gallery.md`; the reference is `docs/OPERATIONS.md` → *A local render* and
`kiosk-mac/README.md`.

## The picture

```
                      ┌─ publish / publish-index ──► git push ──► GitHub Pages  (the site)
 a database ─ renderer┤
 (any node's,         └─ render-local ──► a directory nobody pushes
  or an archive's)                           ├─► the laptop, in a browser: sift, pick
                                             └─► sync-local.sh ──► a wall's own loopback
                                                  (kiosk plays it; counts go to the Worker)
```

The renderer is the same; only the row set and the destination differ. A local render can hold
**held** entries (the site never does), shows them as *held · not published* with no QR code,
and can never be written into a git checkout.

Three rules that save a bad afternoon:

- **Run `render-local` on the node itself**, with the node's venv. `bin/sg` adds the node's own
  `--db` last, and `render-local` refuses a second `--db` for that reason.
- **`--jobs` must be the jobs directory of the database you render.** The sld-gpu snapshot's
  rows say `/home/ubuntu/sketchgen/jobs/N`, which on sld-cloud is sld-cloud's own job N.
- **Look at sketches in Chrome, not in the desktop app's Browser pane**, which blocks the
  sandboxed sketch frame (`net::ERR_BLOCKED_BY_CLIENT`) and shows it black.

On sld-cloud, below, `SG` is:

```bash
SG="$HOME/sketchgen/.venv/bin/python3 $HOME/sketchgen/app/bin/sketchgen"
```

## 1. Sift the sld-gpu archive on the laptop

The rental's 3,255 held sketches, all of them, on your laptop.

```bash
ssh sld-cloud
SG="$HOME/sketchgen/.venv/bin/python3 $HOME/sketchgen/app/bin/sketchgen"
A=~/sketchgen-backups/sld-gpu/sketchgen
$SG control pause --reason "render sld-gpu archive"    # the WebP encoding slows the gate
$SG render-local --db $A/backups/2026-09-27T135015Z/sketchgen.db --jobs $A/jobs \
    --out ~/sketchgen-local/sld-gpu --origin sld-gpu --include held
$SG control resume
```

The first run encodes a WebP strip for every entry (thousands of frames; minutes, not seconds)
and ends with `rendered N of 3255 …; K skipped by the scan`. A second run reuses them and takes
seconds. Expect about 1 GB.

On the laptop, to open it in Chrome:

```bash
rsync -a sld-cloud:sketchgen-local/sld-gpu/ ~/sketchgen-local/sld-gpu/
python3 -m http.server 8765 --bind 127.0.0.1 --directory ~/sketchgen-local/sld-gpu
```

Open `http://127.0.0.1:8765/`. Check:

- the banner reads *Local render of **sld-gpu** · not published · 0 picked*;
- every card has a **Pick** toggle, and entry pages have one too;
- an entry page has the chip *held · not published* and no QR code;
- `kiosk.html` plays them.

Pick a handful, press **Copy picked ids**, and save the text as `picks.txt`: one id per line.
Picks live in this browser only (`localStorage`, under `sketchgen-picks:sld-gpu`), so copy them
out before you change browsers.

**Next, not built yet:** `sketchgen import run --ids picks.txt` brings picked entries into
sld-cloud as held entries to publish (`docs/plans/gpu-fold-in.md`, Packet 1). Until then the
picks file is the record. You can already narrow a render to it with
`render-local … --ids picks.txt`.

## 2. Put a local render on a wall

The first wall is the M4 mini beside the D12 wall. Once, at its keyboard, logged in as the wall's
account:

```bash
xcode-select --install                     # a dialog; the server needs these tools' python3
curl -fsSL https://raw.githubusercontent.com/profcarroll/sketchgen/main/kiosk-mac/get.sh | bash
```

`get.sh` walks through `prep.sh`. Then, from the laptop (with `WALL` as its ssh name):

```bash
ssh WALL 'bash ~/Library/sketchgen-kiosk/kit/install.sh --local'
```

**Give the wall a room.** A `site=` that is not in the gallery's `kiosk_sites` counts nothing,
and the title says `not counting: unknown site`. On sld-cloud, add it to
`~/sketchgen/gallery/config.json`, reusing D12's building so the hours are shared:

```json
"kiosk_sites": {
  "d12":      { "building": "vera-list", "name": "D12 lab" },
  "d12-mini": { "building": "vera-list", "name": "D12 lab, second screen" }
}
```

then commit it in that checkout, as D12's was on 2026-09-24 (`781c47d89`). An uncommitted
edit makes every publish refuse the checkout as dirty. Push it so the site agrees:

```bash
cd ~/sketchgen/gallery && git commit -qam "config: the d12-mini kiosk site" && git push -q
```

The checkout pushes with its own deploy key (`core.sshCommand`).

**Render for the wall**, with `--config-from` (it carries the sites over) and `--write-path` (so
it counts):

```bash
$SG render-local --db $A/backups/2026-09-27T135015Z/sketchgen.db --jobs $A/jobs \
    --out ~/sketchgen-local/sld-gpu --origin sld-gpu --include held \
    --config-from ~/sketchgen/gallery --write-path https://sketchgen-writepath.sketchgen.workers.dev
```

**Sync it and switch the wall**, from the laptop:

```bash
kiosk-mac/sync-local.sh sld-cloud:sketchgen-local/sld-gpu WALL
ssh WALL 'echo "http://127.0.0.1:8090/kiosk.html?unattended=1&site=d12-mini" > ~/Library/sketchgen-kiosk/url \
  && launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk'
ssh WALL kiosk-status
```

`sync-local.sh` ends with `served on 127.0.0.1:8090: "build": "…"`. `kiosk-status` shows
the URL, the release served, and a title like `sketchgen kiosk · d12-mini · #412 · counting ·
56d2c8`.

## 3. Check that it counts

The title saying `counting` means the page will post. To see the Worker receive it, run this on
the laptop after a few slots have played:

```bash
cd ~/sketchgen/writepath && set -a && . ~/.config/sketchgen/cloudflare.env && set +a
npx --yes wrangler@latest d1 execute sketchgen-writepath --remote \
  --command "SELECT site, count(*) AS entries, sum(count) AS views FROM kiosk_views GROUP BY site"
```

A `d12-mini` row appears beside `d12`. A local wall's origin can read counts and post kiosk
views, and nothing else: no sign-in, likes, votes or prompts (`writepath/README.md`).

## 4. Day to day

| to | do |
| --- | --- |
| put a newer render on a wall | render again on the node, then `kiosk-mac/sync-local.sh …` again; the page picks it up within 15 min |
| go back one release | `ssh WALL` and run `ls ~/Library/sketchgen-kiosk/releases` (two are kept), then relink with `python3 -c 'import os; d=os.path.expanduser("~/Library/sketchgen-kiosk"); os.symlink("releases/<older>", d+"/g.new"); os.replace(d+"/g.new", d+"/gallery")'` |
| put the wall back on Pages | `ssh WALL 'rm ~/Library/sketchgen-kiosk/url && launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk'` |
| stop serving the local copy | `ssh WALL 'bash ~/Library/sketchgen-kiosk/kit/install.sh --remove-local'` (after moving the wall back to Pages) |
| deploy the Worker | `cd writepath && set -a && . ~/.config/sketchgen/cloudflare.env && set +a && npx --yes wrangler@latest deploy` (D1 changes first) |
| render the site's own entries locally | `$SG render-local --db ~/sketchgen/sketchgen.db --jobs ~/sketchgen/jobs --out … --origin sld-cloud --include published` |

## When something looks wrong

| you see | it is |
| --- | --- |
| sketches black on the laptop | the Browser pane; open the page in Chrome |
| `refused: --db given twice` | you ran it through `bin/sg`; run it on the node |
| `refused: none of the N entries has a job directory under …` | `--jobs` is not that database's jobs directory |
| `skipped e/N: email-shaped string …` | the personal-data guard; that one entry is left out, the rest render |
| title `not counting: no write path` | rendered without `--write-path` |
| title `not counting: unknown site …` | the `site=` is not in `kiosk_sites` of the render's `config.json`; add it, re-render with `--config-from`, sync |
| counts show `—` on a local page | Worker not deployed, or the page is not on `127.0.0.1:8090`/`localhost:8090` (`KIOSK_ORIGINS`) |
| `served on 127.0.0.1:8090: nothing answering` | the gallery agent is not loaded: `install.sh --local`; its log is `~/Library/Caches/sketchgen-kiosk/gallery.log` |
| `install.sh --local` refuses: no Command Line Tools | `xcode-select --install` at the Mac's keyboard |
