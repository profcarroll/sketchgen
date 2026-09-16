/**
 * sketchgen gallery write path — one small Cloudflare Worker over a D1 database.
 *
 * Why it exists: the gallery is a static GitHub Pages site and cannot write, and
 * the node must never listen on a public address (spec §4). Compare votes, likes
 * and view counts therefore land here, and the node's worker pulls them on its
 * own schedule through /pull. /pull is the ONLY route the node ever calls; the
 * node opens no port for this.
 *
 * It also takes submissions: a prompt or a critique a signed-in visitor asked
 * for. A submission is not a job. It lands in its own table, capped per login
 * per UTC day, and becomes a job only when the operator releases it on the
 * node — so this Worker knows nothing about jobs, entries or the pipeline, and
 * nothing the public types can reach a model by accident.
 *
 * What it knows about a person: a GitHub username. That is the whole list. The
 * OAuth access token is used inside /callback to read the login and is discarded
 * in the same function; nothing else from the GitHub user document is read, kept
 * or derived. No request headers that identify a client are read or stored.
 *
 * A session is a signed token, `username.expiry.HMAC(username.expiry)`, held
 * either in this Worker's cookie or in the gallery's own browser storage and
 * sent back as `Authorization: Bearer`. Both are verified the same way; see
 * sessionToken() for why one of them is not enough.
 *
 * JUDGE BLINDING (spec §5, plan packet 5.2): /counts is deliberately public,
 * because the gallery renders view and like counts to human visitors. The
 * agent-judge code must never call it. Engagement is not judgment: an agent that
 * has seen a like count is no longer answering the same question the humans
 * answered, and the divergence between the two populations is the measurement.
 *
 * Runtime surface: plain Web APIs (Request, Response, URL, crypto.subtle, fetch)
 * and `env.DB.prepare(sql).bind(...).run() / .all() / .first()`. Nothing else, so
 * a fake binding can stand in under `node --test`.
 */

const SESSION_COOKIE = "sg_session";
const STATE_COOKIE = "sg_state";
const SESSION_SECONDS = 30 * 24 * 60 * 60; // 30 days
const STATE_SECONDS = 600;
const VIEW_WINDOW_MS = 60_000;
const PULL_LIMIT = 500;
const QUESTIONS = new Set(["brief", "look"]);
const CHOICES = new Set(["A", "B", "tie"]);

// What one GitHub login may ask for in one UTC day. A vote is idempotent per
// person, so abusing it is self-limiting; a prompt is roughly a minute of the
// node's only GPU, so it is not. The cap has to be able to refuse, which the
// operator's console cannot do after the text has already been submitted.
// Changing either number is this line and the test that names it.
export const PROMPTS_PER_DAY = 3;
export const CRITIQUES_PER_DAY = 5;

// A prompt is a sentence or two, not an essay. The critique has no character
// cap of its own because it has a word one, which is lineage.validate's.
export const MAX_PROMPT_CHARS = 240;
export const MAX_CRITIQUE_WORDS = 40;
const GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize";
const GITHUB_TOKEN = "https://github.com/login/oauth/access_token";
const GITHUB_USER = "https://api.github.com/user";
const USER_AGENT = "sketchgen-writepath";

// ---------------------------------------------------------------------------
// Every statement this Worker issues, in one place, so the tests can assert on
// the statement rather than parse SQL.
// ---------------------------------------------------------------------------

export const SQL = {
  insertState:
    "INSERT INTO oauth_state (state, created_utc) VALUES (?, ?)",
  getState:
    "SELECT state, created_utc FROM oauth_state WHERE state = ?",
  deleteState:
    "DELETE FROM oauth_state WHERE state = ?",
  sweepState:
    "DELETE FROM oauth_state WHERE created_utc < ?",
  upsertVote:
    "INSERT INTO votes (username, entry_a, entry_b, question, choice, created_utc, updated_utc) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?) " +
    "ON CONFLICT(username, entry_a, entry_b, question) " +
    "DO UPDATE SET choice = excluded.choice, updated_utc = excluded.updated_utc",
  setLike:
    "INSERT INTO likes (entry_id, username, active, created_utc, updated_utc) " +
    "VALUES (?, ?, ?, ?, ?) " +
    "ON CONFLICT(entry_id, username) " +
    "DO UPDATE SET active = excluded.active, updated_utc = excluded.updated_utc",
  bumpView:
    "INSERT INTO views (entry_id, count, updated_utc) VALUES (?, 1, ?) " +
    "ON CONFLICT(entry_id) " +
    "DO UPDATE SET count = views.count + 1, updated_utc = excluded.updated_utc",
  lastSeen:
    "SELECT seen_utc FROM view_log WHERE session_hash = ? AND entry_id = ?",
  touchSeen:
    "INSERT INTO view_log (session_hash, entry_id, seen_utc) VALUES (?, ?, ?) " +
    "ON CONFLICT(session_hash, entry_id) DO UPDATE SET seen_utc = excluded.seen_utc",
  pullVotes:
    "SELECT username, entry_a, entry_b, question, choice, created_utc, updated_utc " +
    "FROM votes WHERE updated_utc >= ? " +
    "ORDER BY updated_utc, username, entry_a, entry_b, question LIMIT ?",
  pullLikes:
    "SELECT entry_id, username, active, created_utc, updated_utc " +
    "FROM likes WHERE updated_utc >= ? ORDER BY updated_utc, entry_id, username LIMIT ?",
  pullViews:
    "SELECT entry_id, count, updated_utc " +
    "FROM views WHERE updated_utc >= ? ORDER BY updated_utc, entry_id LIMIT ?",
  insertSubmission:
    "INSERT INTO submissions (kind, username, entry_id, text, created_utc, updated_utc) " +
    "VALUES (?, ?, ?, ?, ?, ?)",
  // The day's budget, straight off the rows: no counter to drift, nothing to
  // reset at midnight, and a row that was never written never counted.
  countToday:
    "SELECT COUNT(*) AS n FROM submissions " +
    "WHERE username = ? AND kind = ? AND created_utc >= ?",
  pullSubmissions:
    "SELECT id, kind, username, entry_id, text, created_utc, updated_utc " +
    "FROM submissions WHERE updated_utc >= ? ORDER BY updated_utc, id LIMIT ?",
};

/** `SELECT … IN (?, ?, …)` for n entry ids. Exported so the tests build the
 *  identical string instead of guessing at the spacing. */
export function sqlCountViews(n) {
  return `SELECT entry_id, count FROM views WHERE entry_id IN (${placeholders(n)})`;
}

export function sqlCountLikes(n) {
  return (
    "SELECT entry_id, COUNT(*) AS n FROM likes WHERE active = 1 AND entry_id IN " +
    `(${placeholders(n)}) GROUP BY entry_id`
  );
}

function placeholders(n) {
  return new Array(n).fill("?").join(", ");
}

// D1's ceiling on bound parameters per statement. Anything that binds one
// parameter per entry id has to come through chunked() to stay under it.
export const BIND_LIMIT = 100;

// The most ids one /counts call may name, whatever the chunking underneath.
export const MAX_COUNT_IDS = 1000;

export function chunked(items, size) {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** Now, UTC, ISO 8601 with a Z, second resolution. The only clock read here. */
function utcNow(ms = Date.now()) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function utcPlus(seconds, ms = Date.now()) {
  return utcNow(ms + seconds * 1000);
}

/** Midnight at the start of the UTC day `ms` falls in. The budget window.
 *
 *  UTC and not the visitor's zone, deliberately: the Worker is not told where
 *  anyone is and is not going to start asking. A day here is the same day for
 *  everybody, which also makes the count a plain string comparison. */
function utcDayStart(ms = Date.now()) {
  return `${new Date(ms).toISOString().slice(0, 10)}T00:00:00Z`;
}

function parseUtc(stamp) {
  const ms = Date.parse(stamp);
  return Number.isNaN(ms) ? null : ms;
}

function b64url(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return hex(digest);
}

async function hmac(message, key) {
  const imported = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    imported,
    new TextEncoder().encode(message),
  );
  return b64url(signature);
}

/** Length-independent, value-constant comparison. */
function sameSecret(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

function randomToken() {
  return hex(crypto.getRandomValues(new Uint8Array(24)));
}

// GitHub logins: alphanumeric and single hyphens. The dot is the session
// separator, so a login can never contain one and the split is unambiguous.
const LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/;

function isEntryId(value) {
  return Number.isInteger(value) && value > 0;
}

// ---------------------------------------------------------------------------
// What a stranger is allowed to submit — lineage.validate, in JavaScript
// ---------------------------------------------------------------------------

/**
 * The marks a sentence of prompt or critique has no business containing.
 *
 * This list is `lineage._CODE_MARKS` (sketchgen/lineage.py) copied by hand and
 * exported so a reader can hold the two against each other, in the same order,
 * and see at a glance that they still match. Keep the order.
 */
export const CODE_MARKS = [
  "```",
  "{",
  "}",
  ";",
  "()",
  "=>",
  "function ",
  "<script",
  "//",
  "$",
];

// Where one sentence ends and the next begins: a terminator, then whitespace.
// `lineage._SENTENCE_SPLIT_RE`, which is the same expression.
const SENTENCE_SPLIT_RE = /(?<=[.!?])\s+/;

/**
 * One line of public text, trimmed, or the sentence that refuses it.
 *
 * Returns `{ text }` or `{ error }`. The rules are `lineage.validate`'s and the
 * sentences are its sentences, word for word, because a visitor who is refused
 * here and a model that is refused on the node should be told the same thing.
 * Three copies of this rule exist on purpose — the gallery page refuses before
 * the round trip, this Worker refuses whatever the page does, and `sync.py`
 * refuses again on the way into the node — so the wording is the contract
 * between them and is not ours to improve.
 *
 * A prompt and a critique differ in exactly two places. A prompt is capped at
 * MAX_PROMPT_CHARS, because nothing else bounds it; that sentence has no
 * Python original, since the Python validator only ever sees a critique. And a
 * prompt is *not* held to the one-sentence rule: a revision line is one
 * sentence because it is an instruction to patch a prompt, while a prompt may
 * describe as much as it likes inside its 240 characters.
 */
export function validateText(raw, kind) {
  const text = String(raw ?? "").split(/\s+/).filter(Boolean).join(" ");
  if (typeof raw !== "string" || !text) {
    return { error: `the ${kind} is empty` };
  }
  for (const mark of CODE_MARKS) {
    if (text.includes(mark)) {
      // Two wordings, because a critique's refusal says what goes wrong: it
      // stops being a patch and becomes a prompt. A prompt is already one.
      return {
        error:
          kind === "critique"
            ? `the critique contains code ('${mark}'); it becomes a prompt, not a patch`
            : `the prompt holds code ('${mark}')`,
      };
    }
  }
  if (kind === "prompt") {
    if (text.length > MAX_PROMPT_CHARS) {
      return {
        error:
          `the prompt is ${text.length} characters; ` +
          `at most ${MAX_PROMPT_CHARS} is the contract`,
      };
    }
    return { text };
  }
  const sentences = text.split(SENTENCE_SPLIT_RE).filter(Boolean);
  if (sentences.length > 1) {
    return { error: `the critique is ${sentences.length} sentences; one is the contract` };
  }
  const words = text.split(" ");
  if (words.length >= MAX_CRITIQUE_WORDS) {
    return {
      error:
        `the critique is ${words.length} words; ` +
        `under ${MAX_CRITIQUE_WORDS} is the contract`,
    };
  }
  return { text };
}

// ---------------------------------------------------------------------------
// Sessions: username.expiry.HMAC(username.expiry)
// ---------------------------------------------------------------------------

export async function signSession(username, expiryEpoch, key) {
  const body = `${username}.${expiryEpoch}`;
  return `${body}.${await hmac(body, key)}`;
}

/** The username carried by a valid, unexpired, untampered token, else null. */
export async function readSession(value, key, nowMs = Date.now()) {
  if (!value) return null;
  const parts = value.split(".");
  if (parts.length !== 3) return null;
  const [username, expiry, signature] = parts;
  if (!LOGIN_RE.test(username)) return null;
  if (!/^\d+$/.test(expiry)) return null;
  if (Number(expiry) * 1000 <= nowMs) return null;
  const expected = await hmac(`${username}.${expiry}`, key);
  return sameSecret(expected, signature) ? username : null;
}

function cookies(request) {
  const header = request.headers.get("Cookie") || "";
  const jar = {};
  for (const piece of header.split(";")) {
    const at = piece.indexOf("=");
    if (at < 1) continue;
    jar[piece.slice(0, at).trim()] = piece.slice(at + 1).trim();
  }
  return jar;
}

function setCookie(name, value, seconds) {
  return `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${seconds}`;
}

/** The bearer token a request offers, or "". */
function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

/**
 * The session token a request offers: the cookie first, then the bearer header.
 *
 * Why both. The gallery is on github.io and this Worker is on workers.dev, so
 * every call the gallery makes is cross-site. A SameSite=Lax cookie is not sent
 * on those, and SameSite=None would be dropped anyway as a third-party cookie
 * by Safari and, increasingly, Chrome — so a cookie alone cannot sign anyone in
 * from the gallery. /callback therefore also hands the page the same signed
 * token in the redirect fragment; the page keeps it in its own origin's storage
 * and sends it back as a bearer. The token is identical either way and is
 * checked by the same HMAC verification, so this is one trust path, not two.
 *
 * Which credential a route accepts is decided by the route, never by the shape
 * of the header: /pull takes env.PULL_TOKEN and nothing else, and a session
 * token presented there is refused like any other wrong value.
 */
function sessionToken(request) {
  return cookies(request)[SESSION_COOKIE] || bearer(request) || null;
}

// ---------------------------------------------------------------------------
// Responses and CORS
// ---------------------------------------------------------------------------

function galleryOrigin(env) {
  try {
    return new URL(env.GALLERY_URL).origin;
  } catch {
    return null;
  }
}

function corsHeaders(request, env) {
  const allowed = galleryOrigin(env);
  const origin = request.headers.get("Origin");
  const headers = { Vary: "Origin" };
  if (allowed && origin === allowed) {
    headers["Access-Control-Allow-Origin"] = allowed;
    headers["Access-Control-Allow-Credentials"] = "true";
  }
  return headers;
}

function json(body, status, request, env, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ...corsHeaders(request, env),
      ...extra,
    },
  });
}

function redirect(location, extra = {}) {
  return new Response(null, { status: 302, headers: { Location: location, ...extra } });
}

function preflight(request, env) {
  const allowed = galleryOrigin(env);
  const origin = request.headers.get("Origin");
  if (!allowed || origin !== allowed) {
    // Any other origin, including a missing one, gets a refusal with no
    // Access-Control-Allow-Origin header, so the browser blocks the call.
    return json({ error: "origin not allowed" }, 403, request, env);
  }
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": allowed,
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      // Authorization, because the gallery sends the session as a bearer: a
      // SameSite=Lax cookie never reaches a cross-site call (see sessionToken).
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      "Access-Control-Max-Age": "600",
      Vary: "Origin",
    },
  });
}

async function readJson(request) {
  try {
    const body = await request.json();
    return body && typeof body === "object" && !Array.isArray(body) ? body : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

async function routeLogin(request, env) {
  const state = randomToken();
  const now = Date.now();
  await env.DB.prepare(SQL.sweepState).bind(utcNow(now - STATE_SECONDS * 1000)).run();
  await env.DB.prepare(SQL.insertState).bind(state, utcNow(now)).run();
  const target = new URL(GITHUB_AUTHORIZE);
  target.searchParams.set("client_id", env.GITHUB_CLIENT_ID);
  target.searchParams.set("state", state);
  // Empty scope: the public profile is all this service is allowed to see.
  target.searchParams.set("scope", "");
  return redirect(target.toString(), {
    "Set-Cookie": setCookie(STATE_COOKIE, state, STATE_SECONDS),
  });
}

async function routeCallback(request, env, url) {
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const cookieState = cookies(request)[STATE_COOKIE];
  if (!code || !state || !sameSecret(state, cookieState || "")) {
    return json({ error: "bad oauth state" }, 400, request, env);
  }
  const stored = await env.DB.prepare(SQL.getState).bind(state).first();
  await env.DB.prepare(SQL.deleteState).bind(state).run();
  if (!stored) return json({ error: "bad oauth state" }, 400, request, env);

  const tokenResponse = await fetch(GITHUB_TOKEN, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
    },
    body: JSON.stringify({
      client_id: env.GITHUB_CLIENT_ID,
      client_secret: env.GITHUB_CLIENT_SECRET,
      code,
    }),
  });
  if (!tokenResponse.ok) {
    return json({ error: "oauth exchange failed" }, 502, request, env);
  }
  const grant = await tokenResponse.json();
  if (!grant || typeof grant.access_token !== "string") {
    return json({ error: "oauth exchange failed" }, 502, request, env);
  }

  // The token lives for exactly the next two statements. It is passed to
  // api.github.com, the one field this service keeps is pulled out of the
  // response, and nothing derived from either is written anywhere.
  const userResponse = await fetch(GITHUB_USER, {
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${grant.access_token}`,
      "User-Agent": USER_AGENT,
    },
  });
  if (!userResponse.ok) {
    return json({ error: "github user lookup failed" }, 502, request, env);
  }
  const username = (await userResponse.json())?.login;
  if (typeof username !== "string" || !LOGIN_RE.test(username)) {
    return json({ error: "unusable github login" }, 502, request, env);
  }

  const expiry = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
  const token = await signSession(username, expiry, env.SESSION_KEY);
  // The cookie is set for same-site visits to this Worker, and the same token
  // rides back to the gallery in the fragment for the cross-site case. A
  // fragment, not a query string: it is never sent to a server, never logged by
  // one, and never lands in a Referer header. The page stores it and strips it
  // from the address bar on load.
  const home = `${env.GALLERY_URL.replace(/\/+$/, "")}/#session=${encodeURIComponent(token)}`;
  return redirect(home, {
    "Set-Cookie": setCookie(SESSION_COOKIE, token, SESSION_SECONDS),
  });
}

function routeLogout(request, env) {
  // The page drops its own copy; this clears the Worker's cookie. There is no
  // session table, so there is nothing else to revoke: the token simply stops
  // being presented, and expires on its own inside 30 days either way.
  return json({ ok: true }, 200, request, env, {
    "Set-Cookie": setCookie(SESSION_COOKIE, "", 0),
  });
}

async function routeVote(request, env, username) {
  const body = await readJson(request);
  if (!body) return json({ error: "bad json" }, 400, request, env);
  const { entry_a: a, entry_b: b, question, choice } = body;
  if (!isEntryId(a) || !isEntryId(b) || a === b) {
    return json({ error: "entry_a and entry_b must be two entry ids" }, 400, request, env);
  }
  if (!QUESTIONS.has(question)) {
    return json({ error: "question must be brief or look" }, 400, request, env);
  }
  if (!CHOICES.has(choice)) {
    return json({ error: "choice must be A, B or tie" }, 400, request, env);
  }
  const now = utcNow();
  await env.DB.prepare(SQL.upsertVote)
    .bind(username, a, b, question, choice, now, now)
    .run();
  return json({ ok: true, updated_utc: now }, 200, request, env);
}

/**
 * How much of today's budget this login has left for this kind of submission.
 *
 * Counted, never remembered: the only state is the rows themselves, so a refused
 * submission costs nothing, a deleted row gives the day back, and there is no
 * midnight job. `created_utc` and the day start are both ISO 8601 with a Z at
 * second resolution, so `>=` on the strings is `>=` on the instants.
 */
async function budgetLeft(env, username, kind, perDay) {
  const row = await env.DB.prepare(SQL.countToday)
    .bind(username, kind, utcDayStart())
    .first();
  return Math.max(0, perDay - Number(row?.n || 0));
}

/** The new row's id. D1 reports it as `meta.last_row_id`, the rowid SQLite
 *  assigned, which for this table is its AUTOINCREMENT primary key. */
async function insertSubmission(env, kind, username, entryId, text, now) {
  const result = await env.DB.prepare(SQL.insertSubmission)
    .bind(kind, username, entryId, text, now, now)
    .run();
  return result?.meta?.last_row_id ?? null;
}

/**
 * A prompt a signed-in visitor wants run. It becomes a row and nothing else.
 *
 * This Worker knows nothing about jobs, entries or the pipeline, and that is
 * the point of the table: a submission is text a person has to release on the
 * node before any model sees it, so nothing written here can be picked up by
 * accident. The wording of every refusal is the wire format's (plan §2).
 */
async function routePrompt(request, env, username) {
  const body = await readJson(request);
  if (!body) return json({ error: "bad json" }, 400, request, env);
  // Validated before the budget is read: text this Worker would not have stored
  // anyway should not cost a database round trip, and must not read as a 429.
  const checked = validateText(body.prompt, "prompt");
  if (checked.error) return json({ error: checked.error }, 400, request, env);

  const left = await budgetLeft(env, username, "prompt", PROMPTS_PER_DAY);
  if (left <= 0) {
    return json(
      { error: `${PROMPTS_PER_DAY} prompts a day`, prompts_left: 0 },
      429,
      request,
      env,
    );
  }
  const now = utcNow();
  const id = await insertSubmission(env, "prompt", username, null, checked.text, now);
  return json(
    { ok: true, id, created_utc: now, prompts_left: left - 1 },
    200,
    request,
    env,
  );
}

/**
 * A revision line for an entry that already exists. The same row, with a parent.
 *
 * `entry_id` is checked only for being a positive integer: this Worker has no
 * entries table and must not pretend to have one. The node refuses an unknown
 * parent when it applies the row, which is the only place the question can
 * honestly be answered.
 */
async function routeCritique(request, env, username) {
  const body = await readJson(request);
  if (!body) return json({ error: "bad json" }, 400, request, env);
  const entryId = body.entry_id;
  if (!isEntryId(entryId)) {
    return json({ error: "entry_id must be an entry id" }, 400, request, env);
  }
  const checked = validateText(body.critique, "critique");
  if (checked.error) return json({ error: checked.error }, 400, request, env);

  const left = await budgetLeft(env, username, "critique", CRITIQUES_PER_DAY);
  if (left <= 0) {
    return json(
      { error: `${CRITIQUES_PER_DAY} critiques a day`, critiques_left: 0 },
      429,
      request,
      env,
    );
  }
  const now = utcNow();
  const id = await insertSubmission(env, "critique", username, entryId, checked.text, now);
  return json(
    { ok: true, id, created_utc: now, critiques_left: left - 1 },
    200,
    request,
    env,
  );
}

async function routeLike(request, env, username) {
  const body = await readJson(request);
  if (!body) return json({ error: "bad json" }, 400, request, env);
  const entryId = body.entry_id;
  const on = body.on;
  if (!isEntryId(entryId)) return json({ error: "entry_id must be an entry id" }, 400, request, env);
  if (typeof on !== "boolean") return json({ error: "on must be true or false" }, 400, request, env);
  const now = utcNow();
  await env.DB.prepare(SQL.setLike).bind(entryId, username, on ? 1 : 0, now, now).run();
  return json({ ok: true, entry_id: entryId, on }, 200, request, env);
}

async function routeView(request, env, username, sessionValue) {
  const body = await readJson(request);
  if (!body) return json({ error: "bad json" }, 400, request, env);
  const entryId = body.entry_id;
  if (!isEntryId(entryId)) return json({ error: "entry_id must be an entry id" }, 400, request, env);

  const now = Date.now();
  const stamp = utcNow(now);
  // Signed-in viewers are de-duplicated for 60 s per entry, keyed by a hash of
  // the cookie. Signed-out viewers are not de-duplicated at all: the only way
  // to do it would be to keep something that identifies the client, and this
  // service keeps nothing of the kind.
  if (sessionValue && username) {
    const key = await sha256Hex(sessionValue);
    const seen = await env.DB.prepare(SQL.lastSeen).bind(key, entryId).first();
    const last = seen ? parseUtc(seen.seen_utc) : null;
    if (last !== null && now - last < VIEW_WINDOW_MS) {
      return json({ ok: true, counted: false }, 200, request, env);
    }
    await env.DB.prepare(SQL.touchSeen).bind(key, entryId, stamp).run();
  }
  await env.DB.prepare(SQL.bumpView).bind(entryId, stamp).run();
  return json({ ok: true, counted: true }, 200, request, env);
}

async function routeCounts(request, env, url) {
  const raw = url.searchParams.get("entries") || "";
  const ids = [];
  for (const piece of raw.split(",")) {
    const trimmed = piece.trim();
    if (!trimmed) continue;
    const value = Number(trimmed);
    if (!isEntryId(value)) {
      return json({ error: "entries must be a comma-separated list of entry ids" }, 400, request, env);
    }
    if (!ids.includes(value)) ids.push(value);
  }
  // A ceiling so one request cannot ask for an unbounded number of statements.
  // Well above any gallery this serves; the page chunks its own asks anyway.
  if (ids.length > MAX_COUNT_IDS) {
    return json({ error: `entries must name at most ${MAX_COUNT_IDS} ids` }, 400, request, env);
  }
  const counts = {};
  for (const id of ids) counts[id] = { views: 0, likes: 0 };
  // D1 binds at most BIND_LIMIT parameters to one statement, and the gallery
  // asks for every entry on the page at once. Past that many entries the whole
  // request used to throw, so every count on the grid read "—". One statement
  // per chunk instead: the answer is the same shape whatever the gallery's size.
  for (const chunk of chunked(ids, BIND_LIMIT)) {
    const views = await env.DB.prepare(sqlCountViews(chunk.length)).bind(...chunk).all();
    for (const row of views?.results || []) {
      counts[row.entry_id] = { views: row.count, likes: counts[row.entry_id].likes };
    }
    const likes = await env.DB.prepare(sqlCountLikes(chunk.length)).bind(...chunk).all();
    for (const row of likes?.results || []) {
      counts[row.entry_id] = { views: counts[row.entry_id].views, likes: row.n };
    }
  }
  return json(counts, 200, request, env);
}

async function routePull(request, env, url) {
  const header = request.headers.get("Authorization") || "";
  const offered = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!env.PULL_TOKEN || !sameSecret(offered, env.PULL_TOKEN)) {
    return json({ error: "pull requires the bearer token" }, 401, request, env);
  }
  const since = url.searchParams.get("since") || "1970-01-01T00:00:00Z";
  if (parseUtc(since) === null) {
    return json({ error: "since must be a UTC ISO 8601 timestamp" }, 400, request, env);
  }
  const votes = (await env.DB.prepare(SQL.pullVotes).bind(since, PULL_LIMIT).all())?.results || [];
  const likes = (await env.DB.prepare(SQL.pullLikes).bind(since, PULL_LIMIT).all())?.results || [];
  const views = (await env.DB.prepare(SQL.pullViews).bind(since, PULL_LIMIT).all())?.results || [];
  // Submissions ride the same watermark as everything else. Nothing ever
  // updates one, so its `updated_utc` is its `created_utc` and stays put; the
  // puller does not have to know that and treats all four arrays alike.
  const submissions =
    (await env.DB.prepare(SQL.pullSubmissions).bind(since, PULL_LIMIT).all())?.results || [];

  // The window is inclusive at both ends: rows are re-delivered rather than
  // risk being skipped when several land in the same second, and sync.py
  // applies every row idempotently. At-least-once, never at-most-once.
  let watermark = since;
  for (const row of [...votes, ...likes, ...views, ...submissions]) {
    if (row.updated_utc > watermark) watermark = row.updated_utc;
  }
  return json(
    { since, next_since: watermark, votes, likes, views, submissions },
    200,
    request,
    env,
  );
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") return preflight(request, env);

    if (request.method === "GET" && path === "/login") return routeLogin(request, env);
    if (request.method === "GET" && path === "/callback") return routeCallback(request, env, url);
    if (request.method === "GET" && path === "/counts") return routeCounts(request, env, url);
    // /pull is answered above the session block on purpose: it is the node's
    // route, it takes env.PULL_TOKEN, and it never looks at a session.
    if (request.method === "GET" && path === "/pull") return routePull(request, env, url);

    const sessionValue = sessionToken(request);
    const username = sessionValue
      ? await readSession(sessionValue, env.SESSION_KEY)
      : null;

    if (request.method === "GET" && path === "/logout") return routeLogout(request, env);

    if (request.method === "GET" && path === "/me") {
      if (!username) return json({ error: "not signed in" }, 401, request, env);
      // The budget comes back with the name so the gallery can draw "2 of 3
      // left today" on the composer without a second call.
      return json(
        {
          username,
          prompts_left: await budgetLeft(env, username, "prompt", PROMPTS_PER_DAY),
          critiques_left: await budgetLeft(env, username, "critique", CRITIQUES_PER_DAY),
        },
        200,
        request,
        env,
      );
    }

    if (request.method === "POST" && path === "/view") {
      return routeView(request, env, username, sessionValue);
    }

    if (request.method === "POST" && (path === "/vote" || path === "/like")) {
      if (!username) {
        return json({ error: "sign in with GitHub first" }, 401, request, env);
      }
      return path === "/vote"
        ? routeVote(request, env, username)
        : routeLike(request, env, username);
    }

    // A prompt and a critique are the same thing at this stage — a sentence a
    // stranger wants run — so they take the same credential as a vote does: a
    // session, and only a session. env.PULL_TOKEN is the node's and opens
    // neither, which is why this sits below the session block and /pull sits
    // above it.
    if (request.method === "POST" && (path === "/prompt" || path === "/critique")) {
      const kind = path === "/prompt" ? "prompt" : "critique";
      if (!username) {
        return json({ error: `sign in to submit a ${kind}` }, 401, request, env);
      }
      return kind === "prompt"
        ? routePrompt(request, env, username)
        : routeCritique(request, env, username);
    }

    return json({ error: "not found" }, 404, request, env);
  },
};
