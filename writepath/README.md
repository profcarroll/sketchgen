# writepath — the one place the gallery can write

The gallery is a static GitHub Pages site and cannot write. The node must not
listen on a public address. Compare votes, likes, view counts and the prompts
and critiques visitors submit therefore go to a small Cloudflare Worker over a
D1 (SQLite) database, and the node's worker pulls them down on its own schedule
with `sketchgen sync`. `GET /pull` is the only route the node ever calls; the
node opens no port for any of this.

- `worker.js` — the Worker. `/login` `/callback` `/me` `/logout` `/vote` `/like`
  `/view` `/counts` `/prompt` `/critique` `/pull`. Its comments, not this file,
  are authoritative.
- `schema.sql` — the D1 tables: `votes`, `likes`, `views`, `kiosk_views`, `view_log`,
  `oauth_state`, `submissions`. `IF NOT EXISTS` throughout and safe to rerun,
  which is how a new table gets there.
- `wrangler.toml` — names and placeholders. No secret is ever written here.
- `test/worker.test.js` — `node --test writepath/test/`. No npm install, no
  network: a fake `env.DB` and a fake `fetch` stand in.
- `../sketchgen/sync.py`, `sketchgen sync` — the node half.

## Privacy

A **GitHub username** is the only thing this service knows about a person. The
OAuth access token is used inside `/callback` to read `login` and is discarded in
the same function; nothing else from the GitHub user document is read or kept —
no email, no display name, no avatar URL. No IP address and no request header
that identifies a client is read or stored.

A **session** is one signed token,
`username.expiry.HMAC-SHA256(username.expiry, SESSION_KEY)`, verified the same
way wherever it comes from. It lives as a cookie on the Worker's domain
(HttpOnly, Secure, SameSite=Lax, 30 days) **and in the viewer's browser storage
for the gallery origin**, under `sketchgen_session` — because the gallery is a
different site, a SameSite=Lax cookie is never sent on its calls, and
SameSite=None would be dropped as a third-party cookie by Safari and
increasingly by Chrome. `/callback` returns the token in the redirect fragment
(never a query string, so it reaches no log and no `Referer`); the page strips it
from the address bar on load and sends it as `Authorization: Bearer`.

The token **carries no secret of the service** — a username, an expiry, a
signature. The signing key never leaves the Worker, the token cannot mint
another, and it opens nothing but this viewer's own votes, likes and
submissions; `/pull`
takes the node's separate `PULL_TOKEN` and refuses a session. There is no
session table to leak: `/logout` clears the cookie and the page drops its copy.
`view_log` holds SHA-256 of the token, never the token. Signed-out views are
**not** de-duplicated, on purpose: the only way would be to keep something that
identifies the viewer.

The kiosk page is signed out and always will be, so nothing here restrains it
and it restrains itself instead (`docs/plans/kiosk-views.md`). It names itself
with `source: "kiosk"` on `/view`, which lands in `views.kiosk_count` — a
subset of `count`, kept so the projector's views can still be told from the
ones a person clicked. A kiosk may also send `site`, the room id from its
launch URL (`?site=d12`, `[a-z0-9-]{1,32}`) — a place, not a person — which
bumps `kiosk_views` for that entry and room in the same batch. `site` on
anything but a kiosk view is refused. `/counts` and `/pull` report `count` and
only `count`, so the gallery and the node see one number, as they always have;
nothing reads `kiosk_count` or `kiosk_views` yet.

`/counts` is public because the gallery shows view and like counts to human
visitors. The agent-judge code (packet 5.2) must never call it — engagement is
not judgment, and an agent that has seen a like count is no longer answering the
question the humans answered.

## Submissions — and why one is not a job

`POST /prompt` takes `{"prompt": "…"}` and `POST /critique` takes
`{"entry_id": 412, "critique": "…"}`. Both need a session, exactly as `/vote`
does; neither accepts `PULL_TOKEN`. Both answer
`{"ok": true, "id": …, "created_utc": "…", "prompts_left"|"critiques_left": …}`,
and `GET /me` carries the same two counts so the page can draw the budget
without a second call. `GET /pull` grows a `submissions` array beside `votes`,
`likes` and `views`, on the same inclusive watermark.

**A submission is not a job.** It lands in the `submissions` table and nothing
else. This Worker knows nothing about jobs, entries or the pipeline, and cannot:
the operator releases a row on the node, and only then does it become a
`publication='hold'` job like any other. Nothing a stranger types can reach a
model because a queue happened to be looking.

**The budget** is `PROMPTS_PER_DAY` and `CRITIQUES_PER_DAY` at the top of
`worker.js` — 3 and 5 — per GitHub login, per **UTC** day. It is counted off the
rows themselves (`COUNT(*)` by username, kind and the start of the day), so
there is no counter to drift, nothing to reset at midnight, and a refused
submission costs nothing. UTC and not the visitor's zone, because the Worker is
not told where anyone is and is not going to start asking.

**The rules on the text** are `lineage.validate`'s, ported to JavaScript with
its sentences word for word: no code marks (`CODE_MARKS`, which is
`lineage._CODE_MARKS` copied by hand and exported so the two can be read side by
side), and for a critique one sentence and under 40 words. A prompt is capped at
240 characters instead and is *not* held to the one-sentence rule — that rule is
the revision line's. Three copies of this validation exist deliberately: the
gallery page refuses before the round trip, this Worker refuses whatever the
page does, and `sync.py` refuses again on the way into the node.

`entry_id` is only checked for being a positive integer. There is no entries
table in this database and pretending to know which ids exist would be a lie;
the node refuses an unknown parent when it applies the row.

Deploying this needs no new secret and no new binding — re-run step 3 below for
the table, then `wrangler deploy`.

## Hand steps, in order

Nothing below has been done. Each step is the instructor's.

1. **GitHub OAuth App** — github.com/settings/developers → New OAuth App.
   Homepage `https://profcarroll.github.io/sketchgen-gallery`, callback
   `https://<worker>.workers.dev/callback`. Keep the client id and generate a
   client secret. (The worker subdomain is known after step 5; register the
   callback then, or create the app now and edit the callback afterwards.)
2. **Database** — `wrangler d1 create sketchgen-writepath`. Paste the printed
   `database_id` into `wrangler.toml` over the placeholder, and commit that.
3. **Schema** —
   `wrangler d1 execute sketchgen-writepath --remote --file=./schema.sql`.

   `schema.sql` is `CREATE TABLE IF NOT EXISTS` throughout, so re-running it
   against a live database adds nothing. A column added to a table that already
   exists is run by hand, **before** the Worker that writes it deploys — a
   Worker whose statement names a column the database has not got fails every
   call to that route. The one such column so far:

   ```
   wrangler d1 execute sketchgen-writepath --remote \
     --command="ALTER TABLE views ADD COLUMN kiosk_count INTEGER NOT NULL DEFAULT 0;"
   ```

   A new table is the same rule: rerun the file, or run the one statement, and
   check the table exists **before** the deploy — the 2026-09-16 deploy put
   routes live ahead of `submissions` and signed every visitor out. The
   per-site kiosk counts (`kiosk_views`, 2026-09-23):

   ```
   wrangler d1 execute sketchgen-writepath --remote \
     --command="CREATE TABLE IF NOT EXISTS kiosk_views (entry_id INTEGER NOT NULL, site TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, updated_utc TEXT NOT NULL, PRIMARY KEY (entry_id, site));"
   wrangler d1 execute sketchgen-writepath --remote \
     --command="SELECT name FROM sqlite_master WHERE name = 'kiosk_views';"
   ```
4. **Secrets** — four, from this directory, each read from `env` at runtime:
   - `wrangler secret put GITHUB_CLIENT_ID` — from step 1
   - `wrangler secret put GITHUB_CLIENT_SECRET` — from step 1
   - `wrangler secret put SESSION_KEY` — 32+ random bytes, e.g.
     `openssl rand -base64 32`. Changing it signs every viewer out.
   - `wrangler secret put PULL_TOKEN` — another `openssl rand -base64 32`; this
     is the bearer the node sends to `/pull`, and nothing else uses it.
5. **Deploy** — `wrangler deploy`. Note the `https://<worker>.workers.dev` URL
   and finish step 1's callback with it.
6. **Pages** — enable GitHub Pages on the `sketchgen-gallery` repo (packet 3.2's
   deploy key pushes to it). Its origin must equal `GALLERY_URL` in
   `wrangler.toml` exactly, or every browser call is refused by CORS.
7. **The token on the node** — put the `PULL_TOKEN` value in
   `~/sketchgen/writepath.token`, then `chmod 600 ~/sketchgen/writepath.token`.
   `sketchgen sync` refuses to run if the file is readable by anyone else, and
   never takes the token on the command line, where `/proc` would expose it.
8. **Schedule it** — one pull per worker cycle:
   `sketchgen sync --once --url https://<worker>.workers.dev`, or set
   `SKETCHGEN_WRITEPATH_URL` and drop the flag. A systemd `--user` timer beside
   `sketchgen-worker.timer` does the same job; creating one is ASK-FIRST.

## Checking it

`sketchgen sync --once --json` prints the counts it applied and the watermark it
reached. The watermark lives in `sync_state`; delivery is at-least-once and
every write is an upsert, so running it twice changes nothing the first run did.
