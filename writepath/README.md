# writepath — the one place the gallery can write

The gallery is a static GitHub Pages site and cannot write. The node must not
listen on a public address. Compare votes, likes and view counts therefore go to
a small Cloudflare Worker over a D1 (SQLite) database, and the node's worker
pulls them down on its own schedule with `sketchgen sync`. `GET /pull` is the
only route the node ever calls; the node opens no port for any of this.

- `worker.js` — the Worker. `/login` `/callback` `/me` `/logout` `/vote` `/like`
  `/view` `/counts` `/pull`. Its comments, not this file, are authoritative.
- `schema.sql` — the D1 tables.
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
another, and it opens nothing but this viewer's own votes and likes; `/pull`
takes the node's separate `PULL_TOKEN` and refuses a session. There is no
session table to leak: `/logout` clears the cookie and the page drops its copy.
`view_log` holds SHA-256 of the token, never the token. Signed-out views are
**not** de-duplicated, on purpose: the only way would be to keep something that
identifies the viewer.

`/counts` is public because the gallery shows view and like counts to human
visitors. The agent-judge code (packet 5.2) must never call it — engagement is
not judgment, and an agent that has seen a like count is no longer answering the
question the humans answered.

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
