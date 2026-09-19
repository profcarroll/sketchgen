/**
 * Unit tests for the gallery write path Worker.
 *
 * Node built-ins only: `node --test writepath/test/`. There is no npm install,
 * no wrangler and no network. `env.DB` is a fake binding over a plain JS store
 * that recognises exactly the statements worker.js issues — it matches on the
 * statement text exported as `SQL`, and never parses SQL. `fetch` is replaced
 * for the one test that exercises /callback.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import worker, {
  SQL,
  signSession,
  readSession,
  sqlCountViews,
  sqlCountLikes,
  BIND_LIMIT,
  MAX_COUNT_IDS,
  CODE_MARKS,
  PROMPTS_PER_DAY,
  CRITIQUES_PER_DAY,
  MAX_PROMPT_CHARS,
  MAX_CRITIQUE_WORDS,
} from "../worker.js";

const GALLERY_URL = "https://profcarroll.github.io/sketchgen-gallery";
const GALLERY_ORIGIN = "https://profcarroll.github.io";
const SESSION_KEY = "test-session-key-not-a-real-one";
const PULL_TOKEN = "test-pull-token";
const WORKER = "https://writepath.example.workers.dev";

// ---------------------------------------------------------------------------
// The fake D1 binding
// ---------------------------------------------------------------------------

function makeDB() {
  const store = {
    votes: new Map(), // `${username}|${a}|${b}|${question}`
    likes: new Map(), // `${entry_id}|${username}`
    views: new Map(), // entry_id
    viewLog: new Map(), // `${session_hash}|${entry_id}`
    oauthState: new Map(), // state
    submissions: new Map(), // id, as SQLite's AUTOINCREMENT hands them out
  };
  let nextSubmissionId = 1;
  const statements = []; // every {sql, args} the worker issued, in order

  const sorted = (rows, keys) =>
    rows.slice().sort((x, y) => {
      for (const key of keys) {
        if (x[key] < y[key]) return -1;
        if (x[key] > y[key]) return 1;
      }
      return 0;
    });

  function execute(sql, args) {
    statements.push({ sql, args });

    switch (sql) {
      case SQL.insertState:
        store.oauthState.set(args[0], { state: args[0], created_utc: args[1] });
        return { rows: [] };
      case SQL.getState:
        return { rows: [store.oauthState.get(args[0])].filter(Boolean) };
      case SQL.deleteState:
        store.oauthState.delete(args[0]);
        return { rows: [] };
      case SQL.sweepState:
        for (const [key, row] of store.oauthState) {
          if (row.created_utc < args[0]) store.oauthState.delete(key);
        }
        return { rows: [] };

      case SQL.upsertVote: {
        const [username, a, b, question, choice, created, updated] = args;
        const key = `${username}|${a}|${b}|${question}`;
        const existing = store.votes.get(key);
        store.votes.set(key, {
          username,
          entry_a: a,
          entry_b: b,
          question,
          choice,
          created_utc: existing ? existing.created_utc : created,
          updated_utc: updated,
        });
        return { rows: [] };
      }

      case SQL.setLike: {
        const [entryId, username, active, created, updated] = args;
        const key = `${entryId}|${username}`;
        const existing = store.likes.get(key);
        store.likes.set(key, {
          entry_id: entryId,
          username,
          active,
          created_utc: existing ? existing.created_utc : created,
          updated_utc: updated,
        });
        return { rows: [] };
      }

      case SQL.bumpView: {
        const [entryId, fromKiosk, updated] = args;
        const existing = store.views.get(entryId);
        store.views.set(entryId, {
          entry_id: entryId,
          count: existing ? existing.count + 1 : 1,
          kiosk_count: (existing ? existing.kiosk_count : 0) + fromKiosk,
          updated_utc: updated,
        });
        return { rows: [] };
      }

      case SQL.lastSeen: {
        const row = store.viewLog.get(`${args[0]}|${args[1]}`);
        return { rows: row ? [row] : [] };
      }
      case SQL.touchSeen:
        store.viewLog.set(`${args[0]}|${args[1]}`, {
          session_hash: args[0],
          entry_id: args[1],
          seen_utc: args[2],
        });
        return { rows: [] };

      case SQL.pullVotes:
        return {
          rows: sorted(
            [...store.votes.values()].filter((r) => r.updated_utc >= args[0]),
            ["updated_utc", "username", "entry_a", "entry_b", "question"],
          ).slice(0, args[1]),
        };
      case SQL.pullLikes:
        return {
          rows: sorted(
            [...store.likes.values()].filter((r) => r.updated_utc >= args[0]),
            ["updated_utc", "entry_id", "username"],
          ).slice(0, args[1]),
        };
      case SQL.pullViews:
        return {
          rows: sorted(
            [...store.views.values()].filter((r) => r.updated_utc >= args[0]),
            ["updated_utc", "entry_id"],
          ).slice(0, args[1]),
        };

      case SQL.insertSubmission: {
        const [kind, username, entryId, text, created, updated] = args;
        const id = nextSubmissionId;
        nextSubmissionId += 1;
        store.submissions.set(id, {
          id,
          kind,
          username,
          entry_id: entryId,
          text,
          created_utc: created,
          updated_utc: updated,
        });
        // What D1 reports for an INSERT: the rowid SQLite assigned.
        return { rows: [], lastRowId: id };
      }
      case SQL.countToday: {
        const [username, kind, dayStart] = args;
        const n = [...store.submissions.values()].filter(
          (r) => r.username === username && r.kind === kind && r.created_utc >= dayStart,
        ).length;
        return { rows: [{ n }] };
      }
      case SQL.pullSubmissions:
        return {
          rows: sorted(
            [...store.submissions.values()].filter((r) => r.updated_utc >= args[0]),
            ["updated_utc", "id"],
          ).slice(0, args[1]),
        };

      default:
        break;
    }

    if (sql === sqlCountViews(args.length)) {
      return {
        rows: [...store.views.values()]
          .filter((r) => args.includes(r.entry_id))
          .map((r) => ({ entry_id: r.entry_id, count: r.count })),
      };
    }
    if (sql === sqlCountLikes(args.length)) {
      const tally = new Map();
      for (const row of store.likes.values()) {
        if (row.active !== 1 || !args.includes(row.entry_id)) continue;
        tally.set(row.entry_id, (tally.get(row.entry_id) || 0) + 1);
      }
      return { rows: [...tally].map(([entry_id, n]) => ({ entry_id, n })) };
    }

    throw new Error(`the worker issued a statement the fake does not know: ${sql}`);
  }

  const DB = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() {
              const result = execute(sql, args);
              return { success: true, meta: { last_row_id: result.lastRowId ?? null } };
            },
            async all() {
              return { results: execute(sql, args).rows, success: true };
            },
            async first() {
              return execute(sql, args).rows[0] ?? null;
            },
          };
        },
      };
    },
  };
  return { DB, store, statements };
}

function makeEnv(extra = {}) {
  const db = makeDB();
  return {
    env: {
      DB: db.DB,
      GALLERY_URL,
      SESSION_KEY,
      PULL_TOKEN,
      GITHUB_CLIENT_ID: "test-client-id",
      GITHUB_CLIENT_SECRET: "test-client-secret",
      ...extra,
    },
    ...db,
  };
}

// ---------------------------------------------------------------------------
// Request helpers
// ---------------------------------------------------------------------------

/** A session cookie that verifies under the test SESSION_KEY. */
async function forgeSession(username, seconds = 3600) {
  return signSession(username, Math.floor(Date.now() / 1000) + seconds, SESSION_KEY);
}

function post(path, body, cookie) {
  const headers = { "Content-Type": "application/json", Origin: GALLERY_ORIGIN };
  if (cookie) headers.Cookie = `sg_session=${cookie}`;
  return new Request(`${WORKER}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

function get(path, { cookie, bearer, origin = GALLERY_ORIGIN } = {}) {
  const headers = {};
  if (origin) headers.Origin = origin;
  if (cookie) headers.Cookie = `sg_session=${cookie}`;
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  return new Request(`${WORKER}${path}`, { headers });
}

// ---------------------------------------------------------------------------
// Votes
// ---------------------------------------------------------------------------

test("a vote without a session is refused", async () => {
  const { env, store, statements } = makeEnv();
  const response = await worker.fetch(
    post("/vote", { entry_a: 1, entry_b: 2, question: "brief", choice: "A" }),
    env,
  );
  assert.equal(response.status, 401);
  assert.equal(response.headers.get("Content-Type"), "application/json");
  assert.equal(store.votes.size, 0);
  assert.deepEqual(statements, []);
});

test("a valid vote is stored, and a second answer updates the same row", async () => {
  const { env, store, statements } = makeEnv();
  const cookie = await forgeSession("octocat");

  const first = await worker.fetch(
    post("/vote", { entry_a: 1, entry_b: 2, question: "brief", choice: "A" }, cookie),
    env,
  );
  assert.equal(first.status, 200);
  assert.equal(store.votes.size, 1);

  const second = await worker.fetch(
    post("/vote", { entry_a: 1, entry_b: 2, question: "brief", choice: "tie" }, cookie),
    env,
  );
  assert.equal(second.status, 200);

  // One row, not two: the (username, pair, question) key is answered once.
  assert.equal(store.votes.size, 1);
  const row = store.votes.get("octocat|1|2|brief");
  assert.equal(row.choice, "tie");
  assert.equal(row.username, "octocat");
  assert.deepEqual(
    statements.map((s) => s.sql),
    [SQL.upsertVote, SQL.upsertVote],
  );

  // The other question about the same pair is a separate answer.
  await worker.fetch(
    post("/vote", { entry_a: 1, entry_b: 2, question: "look", choice: "B" }, cookie),
    env,
  );
  assert.equal(store.votes.size, 2);
});

test("a tampered session cookie is refused", async () => {
  const { env, store } = makeEnv();
  const cookie = await forgeSession("octocat");
  const [name, expiry, signature] = cookie.split(".");

  for (const forged of [
    `mallory.${expiry}.${signature}`, // another name, the real signature
    `${name}.${expiry}.${signature.slice(0, -1)}x`, // a bent signature
    `${name}.${Number(expiry) + 86400}.${signature}`, // a stretched expiry
    `${name}.${Math.floor(Date.now() / 1000) - 10}.${signature}`, // expired
  ]) {
    const response = await worker.fetch(
      post("/vote", { entry_a: 1, entry_b: 2, question: "brief", choice: "A" }, forged),
      env,
    );
    assert.equal(response.status, 401, `accepted ${forged}`);
  }
  assert.equal(store.votes.size, 0);

  // And a valid cookie still works, so the test is not passing by accident.
  const ok = await worker.fetch(
    post("/vote", { entry_a: 1, entry_b: 2, question: "brief", choice: "A" }, cookie),
    env,
  );
  assert.equal(ok.status, 200);
});

// ---------------------------------------------------------------------------
// Likes
// ---------------------------------------------------------------------------

test("likes are idempotent on and off", async () => {
  const { env, store } = makeEnv();
  const cookie = await forgeSession("octocat");

  for (let i = 0; i < 3; i += 1) {
    const response = await worker.fetch(post("/like", { entry_id: 7, on: true }, cookie), env);
    assert.equal(response.status, 200);
  }
  assert.equal(store.likes.size, 1);
  assert.equal(store.likes.get("7|octocat").active, 1);

  for (let i = 0; i < 3; i += 1) {
    const response = await worker.fetch(post("/like", { entry_id: 7, on: false }, cookie), env);
    assert.equal(response.status, 200);
  }
  assert.equal(store.likes.size, 1);
  assert.equal(store.likes.get("7|octocat").active, 0);

  const anonymous = await worker.fetch(post("/like", { entry_id: 7, on: true }), env);
  assert.equal(anonymous.status, 401);
});

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

test("views increment, repeat within 60 s from one session is ignored, anonymous always counts", async () => {
  const { env, store } = makeEnv();
  const cookie = await forgeSession("octocat");

  const first = await worker.fetch(post("/view", { entry_id: 3 }, cookie), env);
  assert.equal(first.status, 200);
  assert.deepEqual(await first.json(), { ok: true, counted: true });
  assert.equal(store.views.get(3).count, 1);

  const again = await worker.fetch(post("/view", { entry_id: 3 }, cookie), env);
  assert.deepEqual(await again.json(), { ok: true, counted: false });
  assert.equal(store.views.get(3).count, 1);

  // A different entry from the same session is a different view.
  await worker.fetch(post("/view", { entry_id: 4 }, cookie), env);
  assert.equal(store.views.get(4).count, 1);

  // A different session is a different viewer.
  const other = await forgeSession("hubot");
  await worker.fetch(post("/view", { entry_id: 3 }, other), env);
  assert.equal(store.views.get(3).count, 2);

  // Signed out: no session, no de-duplication, every beacon counts.
  for (let i = 0; i < 3; i += 1) {
    const response = await worker.fetch(post("/view", { entry_id: 3 }), env);
    assert.deepEqual(await response.json(), { ok: true, counted: true });
  }
  assert.equal(store.views.get(3).count, 5);
  assert.equal(store.viewLog.size, 3); // octocat×2, hubot×1; nothing anonymous
  // Everything above came from an entry page, so none of it is the kiosk's.
  assert.equal(store.views.get(3).kiosk_count, 0);
});

test("a kiosk view is a view, and is also counted as the kiosk's", async () => {
  const { env, store } = makeEnv();

  await worker.fetch(post("/view", { entry_id: 3, source: "kiosk" }), env);
  assert.deepEqual(store.views.get(3), {
    entry_id: 3,
    count: 1,
    kiosk_count: 1,
    updated_utc: store.views.get(3).updated_utc,
  });

  // An entry-page view on the same row moves the total and not the subset, so
  // `count` stays what /counts and /pull report and `kiosk_count` stays the
  // part of it that came from a projector.
  await worker.fetch(post("/view", { entry_id: 3, source: "entry" }), env);
  assert.equal(store.views.get(3).count, 2);
  assert.equal(store.views.get(3).kiosk_count, 1);

  // An absent source is the entry page: this is what gallery.js has always
  // sent, and it has to keep meaning what it meant.
  await worker.fetch(post("/view", { entry_id: 3 }), env);
  assert.equal(store.views.get(3).count, 3);
  assert.equal(store.views.get(3).kiosk_count, 1);
});

test("a source the Worker does not know is refused, and counts nothing", async () => {
  const { env, store } = makeEnv();
  for (const source of ["projector", "", 7, true, { source: "kiosk" }]) {
    const response = await worker.fetch(post("/view", { entry_id: 3, source }), env);
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { error: "source must be entry or kiosk" });
  }
  assert.equal(store.views.get(3), undefined);
});

test("a kiosk view is anonymous and is never de-duplicated", async () => {
  const { env, store } = makeEnv();
  // The projector signs nobody in, so it presents no session and the 60 s
  // window cannot apply to it. kiosk.js is the only thing standing between
  // this endpoint and a view a second (docs/plans/kiosk-views.md §2).
  for (let i = 0; i < 4; i += 1) {
    const response = await worker.fetch(post("/view", { entry_id: 9, source: "kiosk" }), env);
    assert.deepEqual(await response.json(), { ok: true, counted: true });
  }
  assert.equal(store.views.get(9).count, 4);
  assert.equal(store.views.get(9).kiosk_count, 4);
  assert.equal(store.viewLog.size, 0);
});

// ---------------------------------------------------------------------------
// Counts
// ---------------------------------------------------------------------------

test("/counts is public and returns views and likes per entry", async () => {
  const { env, store } = makeEnv();
  store.views.set(1, { entry_id: 1, count: 12, updated_utc: "2026-09-14T00:00:01Z" });
  store.views.set(2, { entry_id: 2, count: 3, updated_utc: "2026-09-14T00:00:02Z" });
  store.likes.set("1|octocat", {
    entry_id: 1, username: "octocat", active: 1,
    created_utc: "2026-09-14T00:00:01Z", updated_utc: "2026-09-14T00:00:01Z",
  });
  store.likes.set("1|hubot", {
    entry_id: 1, username: "hubot", active: 1,
    created_utc: "2026-09-14T00:00:01Z", updated_utc: "2026-09-14T00:00:01Z",
  });
  store.likes.set("2|hubot", {
    entry_id: 2, username: "hubot", active: 0, // unliked: not counted
    created_utc: "2026-09-14T00:00:01Z", updated_utc: "2026-09-14T00:00:03Z",
  });

  const response = await worker.fetch(get("/counts?entries=1,2,3"), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "application/json");
  assert.deepEqual(await response.json(), {
    1: { views: 12, likes: 2 },
    2: { views: 3, likes: 0 },
    3: { views: 0, likes: 0 },
  });

  const bad = await worker.fetch(get("/counts?entries=1,notanumber"), env);
  assert.equal(bad.status, 400);
});

test("/counts answers a gallery larger than the database's bind limit", async () => {
  const { env, store, statements } = makeEnv();
  const ids = [];
  for (let id = 1; id <= BIND_LIMIT * 2 + 7; id++) {
    ids.push(id);
    store.views.set(id, { entry_id: id, count: id, updated_utc: "2026-09-14T00:00:01Z" });
  }

  const response = await worker.fetch(get(`/counts?entries=${ids.join(",")}`), env);
  assert.equal(response.status, 200);

  // Every id asked for is answered, not just the first chunk's worth.
  const body = await response.json();
  assert.equal(Object.keys(body).length, ids.length);
  for (const id of ids) assert.deepEqual(body[id], { views: id, likes: 0 });

  // And no single statement went over the limit that made this fail before.
  assert.ok(statements.length > 2);
  for (const statement of statements) {
    assert.ok(
      statement.args.length <= BIND_LIMIT,
      `a statement bound ${statement.args.length} parameters`,
    );
  }
});

test("/counts refuses an ask naming more ids than it will answer", async () => {
  const { env, statements } = makeEnv();
  const ids = [];
  for (let id = 1; id <= MAX_COUNT_IDS + 1; id++) ids.push(id);

  const response = await worker.fetch(get(`/counts?entries=${ids.join(",")}`), env);
  assert.equal(response.status, 400);
  assert.deepEqual(statements, []);
});

// ---------------------------------------------------------------------------
// Pull — the only route the node calls
// ---------------------------------------------------------------------------

function seedForPull(store) {
  store.votes.set("octocat|1|2|brief", {
    username: "octocat", entry_a: 1, entry_b: 2, question: "brief", choice: "A",
    created_utc: "2026-09-14T00:00:01Z", updated_utc: "2026-09-14T00:00:01Z",
  });
  store.votes.set("hubot|1|2|look", {
    username: "hubot", entry_a: 1, entry_b: 2, question: "look", choice: "B",
    created_utc: "2026-09-14T00:00:09Z", updated_utc: "2026-09-14T00:00:09Z",
  });
  store.likes.set("1|octocat", {
    entry_id: 1, username: "octocat", active: 1,
    created_utc: "2026-09-14T00:00:05Z", updated_utc: "2026-09-14T00:00:05Z",
  });
  store.views.set(1, { entry_id: 1, count: 4, updated_utc: "2026-09-14T00:00:07Z" });
}

test("/pull needs the bearer token", async () => {
  const { env, store } = makeEnv();
  seedForPull(store);

  const none = await worker.fetch(get("/pull?since=1970-01-01T00:00:00Z"), env);
  assert.equal(none.status, 401);

  const wrong = await worker.fetch(
    get("/pull?since=1970-01-01T00:00:00Z", { bearer: "not-the-token" }),
    env,
  );
  assert.equal(wrong.status, 401);
});

test("/pull returns rows at or after the watermark, and the next watermark", async () => {
  const { env, store } = makeEnv();
  seedForPull(store);

  const all = await worker.fetch(
    get("/pull?since=1970-01-01T00:00:00Z", { bearer: PULL_TOKEN }),
    env,
  );
  assert.equal(all.status, 200);
  const first = await all.json();
  assert.equal(first.votes.length, 2);
  assert.equal(first.likes.length, 1);
  assert.equal(first.views.length, 1);
  assert.equal(first.next_since, "2026-09-14T00:00:09Z");
  assert.equal(first.votes[0].username, "octocat"); // ordered by updated_utc

  // Pulling again from the watermark re-delivers only what sits on it.
  const again = await worker.fetch(
    get(`/pull?since=${first.next_since}`, { bearer: PULL_TOKEN }),
    env,
  );
  const second = await again.json();
  assert.equal(second.votes.length, 1);
  assert.equal(second.votes[0].username, "hubot");
  assert.equal(second.likes.length, 0);
  assert.equal(second.views.length, 0);
  assert.equal(second.next_since, "2026-09-14T00:00:09Z");

  // Nothing new after the last row.
  const empty = await worker.fetch(
    get("/pull?since=2026-09-14T00:00:10Z", { bearer: PULL_TOKEN }),
    env,
  );
  const third = await empty.json();
  assert.deepEqual([third.votes, third.likes, third.views], [[], [], []]);
  assert.equal(third.next_since, "2026-09-14T00:00:10Z");

  // Nothing in any pulled row identifies a person beyond the username.
  for (const row of [...first.votes, ...first.likes]) {
    assert.deepEqual(
      Object.keys(row).filter((k) => !/^(entry_a|entry_b|entry_id|question|choice|active|username|created_utc|updated_utc)$/.test(k)),
      [],
    );
  }
});

// ---------------------------------------------------------------------------
// CORS
// ---------------------------------------------------------------------------

test("preflight is allowed from the gallery origin and refused from any other", async () => {
  const { env } = makeEnv();

  const allowed = await worker.fetch(
    new Request(`${WORKER}/vote`, {
      method: "OPTIONS",
      headers: {
        Origin: GALLERY_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
      },
    }),
    env,
  );
  assert.equal(allowed.status, 204);
  assert.equal(allowed.headers.get("Access-Control-Allow-Origin"), GALLERY_ORIGIN);
  assert.equal(allowed.headers.get("Access-Control-Allow-Credentials"), "true");

  const refused = await worker.fetch(
    new Request(`${WORKER}/vote`, {
      method: "OPTIONS",
      headers: { Origin: "https://example.invalid", "Access-Control-Request-Method": "POST" },
    }),
    env,
  );
  assert.equal(refused.status, 403);
  assert.equal(refused.headers.get("Access-Control-Allow-Origin"), null);

  // A POST from another origin gets no allow header either, so the browser
  // never hands the response to the page.
  const cookie = await forgeSession("octocat");
  const cross = await worker.fetch(
    new Request(`${WORKER}/vote`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: "https://example.invalid",
        Cookie: `sg_session=${cookie}`,
      },
      body: JSON.stringify({ entry_a: 1, entry_b: 2, question: "brief", choice: "A" }),
    }),
    env,
  );
  assert.equal(cross.headers.get("Access-Control-Allow-Origin"), null);
});

test("anything else is 404 JSON", async () => {
  const { env } = makeEnv();
  const response = await worker.fetch(get("/admin"), env);
  assert.equal(response.status, 404);
  assert.equal(response.headers.get("Content-Type"), "application/json");
});

// ---------------------------------------------------------------------------
// OAuth: the login is the only thing that survives /callback
// ---------------------------------------------------------------------------

test("/callback keeps only the login and discards everything else GitHub sends", async () => {
  const { env, store, statements } = makeEnv();

  // The GitHub user document as GitHub really sends it: a login buried in a
  // pile of personal data this service must not keep.
  const PRIVATE_FIELDS = {
    id: 583231,
    email: "octocat@users.noreply.github.invalid",
    name: "The Octocat",
    avatar_url: "https://avatars.example.invalid/u/583231",
    location: "San Francisco",
    company: "GitHub",
  };
  const ACCESS_TOKEN = "gho_thisisthetesttokenanditmustnotbestored";
  const seen = [];

  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    if (String(url).includes("access_token")) {
      return new Response(
        JSON.stringify({ access_token: ACCESS_TOKEN, token_type: "bearer", scope: "" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify({ login: "octocat", ...PRIVATE_FIELDS }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    const login = await worker.fetch(get("/login"), env);
    assert.equal(login.status, 302);
    const authorize = new URL(login.headers.get("Location"));
    assert.equal(authorize.origin + authorize.pathname, "https://github.com/login/oauth/authorize");
    assert.equal(authorize.searchParams.get("scope"), ""); // public profile only
    const state = authorize.searchParams.get("state");
    const stateCookie = /sg_state=([^;]+)/.exec(login.headers.get("Set-Cookie"))[1];
    assert.equal(stateCookie, state);
    assert.equal(store.oauthState.size, 1);

    const callback = await worker.fetch(
      new Request(`${WORKER}/callback?code=abc123&state=${state}`, {
        headers: { Cookie: `sg_state=${stateCookie}` },
      }),
      env,
    );
    assert.equal(callback.status, 302);
    assert.ok(
      callback.headers.get("Location").startsWith(`${GALLERY_URL}/#session=`),
      callback.headers.get("Location"),
    );
    assert.equal(store.oauthState.size, 0); // state consumed

    const session = /sg_session=([^;]+)/.exec(callback.headers.get("Set-Cookie"))[1];
    assert.equal(await readSession(session, SESSION_KEY), "octocat");

    // The cookie carries a username, an expiry and a signature. Nothing else.
    const [name, expiry, signature] = session.split(".");
    assert.equal(name, "octocat");
    assert.match(expiry, /^\d+$/);
    assert.ok(signature.length > 0);

    // Neither the cookie, the redirect, nor any statement the worker issued
    // carries an email address, an avatar URL, a real name, or the token.
    const everything = JSON.stringify({
      cookie: callback.headers.get("Set-Cookie"),
      location: callback.headers.get("Location"),
      statements,
      store: {
        votes: [...store.votes.values()],
        likes: [...store.likes.values()],
        oauthState: [...store.oauthState.values()],
      },
    });
    assert.ok(!everything.includes("@"), "an email address survived /callback");
    for (const value of [...Object.values(PRIVATE_FIELDS).map(String), ACCESS_TOKEN]) {
      assert.ok(!everything.includes(value), `${value} survived /callback`);
    }

    // The token did reach api.github.com, and went no further than that call.
    assert.equal(seen.length, 2);
    assert.ok(seen[1].url.startsWith("https://api.github.com/user"));
    assert.equal(seen[1].init.headers.Authorization, `Bearer ${ACCESS_TOKEN}`);

    // /me now answers with the username and the day's budget, and nothing
    // else: the budget is two counts off this login's own rows, not a fact
    // about the person.
    const me = await worker.fetch(get("/me", { cookie: session }), env);
    assert.equal(me.status, 200);
    assert.deepEqual(await me.json(), {
      username: "octocat",
      prompts_left: PROMPTS_PER_DAY,
      critiques_left: CRITIQUES_PER_DAY,
    });
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("/callback refuses a state that does not match the cookie", async () => {
  const { env } = makeEnv();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("/callback must not reach GitHub with a bad state");
  };
  try {
    const response = await worker.fetch(
      new Request(`${WORKER}/callback?code=abc123&state=forged`, {
        headers: { Cookie: "sg_state=something-else" },
      }),
      env,
    );
    assert.equal(response.status, 400);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("/me without a session is 401", async () => {
  const { env } = makeEnv();
  const response = await worker.fetch(get("/me"), env);
  assert.equal(response.status, 401);
});

// ---------------------------------------------------------------------------
// The session as a bearer token — the cross-site half of sign-in
// ---------------------------------------------------------------------------

test("/callback hands the gallery the same token in the fragment", async () => {
  const { env } = makeEnv();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) =>
    new Response(
      JSON.stringify(
        String(url).includes("access_token")
          ? { access_token: "gho_test", token_type: "bearer" }
          : { login: "octocat" },
      ),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  try {
    const login = await worker.fetch(get("/login"), env);
    const state = new URL(login.headers.get("Location")).searchParams.get("state");
    const callback = await worker.fetch(
      new Request(`${WORKER}/callback?code=abc123&state=${state}`, {
        headers: { Cookie: `sg_state=${state}` },
      }),
      env,
    );

    const location = callback.headers.get("Location");
    const fragment = location.slice(`${GALLERY_URL}/#session=`.length);
    const token = decodeURIComponent(fragment);
    assert.notEqual(fragment, ""); // the page has something to store
    assert.equal(await readSession(token, SESSION_KEY), "octocat");

    // It is the same token the cookie carries, so there is one trust path.
    const cookie = /sg_session=([^;]+)/.exec(callback.headers.get("Set-Cookie"))[1];
    assert.equal(token, cookie);

    // And the Worker accepts it as a bearer, which is the whole point: a
    // SameSite=Lax cookie never reaches a cross-site call from the gallery.
    const me = await worker.fetch(get("/me", { bearer: token }), env);
    assert.equal(me.status, 200);
    assert.deepEqual(await me.json(), {
      username: "octocat",
      prompts_left: PROMPTS_PER_DAY,
      critiques_left: CRITIQUES_PER_DAY,
    });
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("a bearer session works on every authenticated route", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");
  const auth = { Authorization: `Bearer ${token}` };
  const body = (path, payload) =>
    new Request(`${WORKER}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Origin: GALLERY_ORIGIN, ...auth },
      body: JSON.stringify(payload),
    });

  assert.equal(
    (await worker.fetch(body("/vote", { entry_a: 1, entry_b: 2, question: "brief", choice: "A" }), env)).status,
    200,
  );
  assert.equal((await worker.fetch(body("/like", { entry_id: 7, on: true }), env)).status, 200);
  assert.equal(store.votes.get("octocat|1|2|brief").choice, "A");
  assert.equal(store.likes.get("7|octocat").active, 1);

  // /view needs no session, but a bearer one de-duplicates the same way a
  // cookie one does: the window is keyed on the token, whichever way it came.
  assert.deepEqual(
    await (await worker.fetch(body("/view", { entry_id: 3 }), env)).json(),
    { ok: true, counted: true },
  );
  assert.deepEqual(
    await (await worker.fetch(body("/view", { entry_id: 3 }), env)).json(),
    { ok: true, counted: false },
  );
  assert.equal(store.views.get(3).count, 1);
});

test("a bearer with a bad signature is refused", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");
  const [name, expiry, signature] = token.split(".");

  for (const forged of [
    `${name}.${expiry}.${signature.slice(0, -1)}x`,
    `mallory.${expiry}.${signature}`,
    "not-a-token",
    await signSession("octocat", Math.floor(Date.now() / 1000) + 3600, "the-wrong-key"),
  ]) {
    const response = await worker.fetch(
      new Request(`${WORKER}/like`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${forged}` },
        body: JSON.stringify({ entry_id: 7, on: true }),
      }),
      env,
    );
    assert.equal(response.status, 401, `accepted ${forged}`);
    assert.equal((await worker.fetch(get("/me", { bearer: forged }), env)).status, 401);
  }
  assert.equal(store.likes.size, 0);
});

test("/pull takes the PULL_TOKEN and not a session, whatever the header shape", async () => {
  const { env, store } = makeEnv();
  seedForPull(store);
  const token = await forgeSession("octocat");

  // A perfectly valid session is still not the node's credential.
  assert.equal(
    (await worker.fetch(get("/pull?since=1970-01-01T00:00:00Z", { bearer: token }), env)).status,
    401,
  );
  // And the node's credential is not a session: it opens no other route.
  assert.equal(
    (await worker.fetch(get("/me", { bearer: PULL_TOKEN }), env)).status,
    401,
  );
  assert.equal(
    (await worker.fetch(get("/pull?since=1970-01-01T00:00:00Z", { bearer: PULL_TOKEN }), env)).status,
    200,
  );
});

test("preflight admits the Authorization header", async () => {
  const { env } = makeEnv();
  const response = await worker.fetch(
    new Request(`${WORKER}/like`, {
      method: "OPTIONS",
      headers: {
        Origin: GALLERY_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization, content-type",
      },
    }),
    env,
  );
  assert.equal(response.status, 204);
  const allowed = (response.headers.get("Access-Control-Allow-Headers") || "")
    .toLowerCase()
    .split(",")
    .map((piece) => piece.trim());
  assert.ok(allowed.includes("authorization"), "the bearer would never be sent");
  assert.ok(allowed.includes("content-type"));
});

test("/logout answers JSON and clears the cookie", async () => {
  const { env } = makeEnv();
  const token = await forgeSession("octocat");
  const response = await worker.fetch(get("/logout", { cookie: token }), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "application/json");
  assert.deepEqual(await response.json(), { ok: true });
  assert.match(response.headers.get("Set-Cookie"), /^sg_session=;/);
  assert.match(response.headers.get("Set-Cookie"), /Max-Age=0/);
});

// ---------------------------------------------------------------------------
// Submissions — a prompt or a critique, which is not a job
// ---------------------------------------------------------------------------

/** A POST carrying the session as a bearer, the way the gallery sends it. */
function postAuth(path, body, token) {
  const headers = { "Content-Type": "application/json", Origin: GALLERY_ORIGIN };
  if (token) headers.Authorization = `Bearer ${token}`;
  return new Request(`${WORKER}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

const A_PROMPT = "a tide of small triangles that drifts toward the cursor";
const A_CRITIQUE = "let the lines thin as they near the edge";

/** N words, one sentence, no code mark: the shape the length rules are about. */
function words(n) {
  return new Array(n).fill("line").join(" ");
}

test("the code marks are lineage._CODE_MARKS, in the same order", () => {
  // Copied by hand from sketchgen/lineage.py, `_CODE_MARKS`. The point of this
  // assertion is that the two lists sit here side by side and a reader can see
  // that they still match; if the Python list gains a mark, this fails.
  assert.deepEqual(CODE_MARKS, [
    "```", "{", "}", ";", "()", "=>", "function ", "<script", "//", "$",
  ]);
});

test("a prompt needs a session, and the node's token is not one", async () => {
  const { env, store, statements } = makeEnv();

  const none = await worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }), env);
  assert.equal(none.status, 401);
  assert.deepEqual(await none.json(), { error: "sign in to submit a prompt" });

  // PULL_TOKEN opens /pull and nothing else. It is not a session and is not
  // treated as one just because it arrives in the same header.
  const node = await worker.fetch(
    postAuth("/prompt", { prompt: A_PROMPT }, PULL_TOKEN),
    env,
  );
  assert.equal(node.status, 401);

  const signedOutCritique = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: A_CRITIQUE }), env,
  );
  assert.equal(signedOutCritique.status, 401);
  assert.deepEqual(await signedOutCritique.json(), { error: "sign in to submit a critique" });

  // None of the three touched the database at all.
  assert.equal(store.submissions.size, 0);
  assert.deepEqual(statements, []);
});

test("a signed-in prompt is one row, and the receipt names it", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  const response = await worker.fetch(
    postAuth("/prompt", { prompt: `  ${A_PROMPT}\n ` }, token),
    env,
  );
  assert.equal(response.status, 200);
  const receipt = await response.json();
  assert.equal(receipt.ok, true);
  assert.equal(receipt.prompts_left, PROMPTS_PER_DAY - 1);
  assert.match(receipt.created_utc, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);

  assert.equal(store.submissions.size, 1);
  const row = store.submissions.get(receipt.id); // the id the receipt gave back
  assert.equal(row.kind, "prompt");
  assert.equal(row.username, "octocat");
  assert.equal(row.entry_id, null); // a prompt has no parent
  assert.equal(row.text, A_PROMPT); // whitespace collapsed, as validate() does
  assert.equal(row.updated_utc, row.created_utc); // it rides the same watermark
});

test("a signed-in critique is one row, with its parent on it", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  const response = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: A_CRITIQUE }, token),
    env,
  );
  assert.equal(response.status, 200);
  const receipt = await response.json();
  assert.equal(receipt.critiques_left, CRITIQUES_PER_DAY - 1);

  const row = store.submissions.get(receipt.id);
  assert.equal(row.kind, "critique");
  assert.equal(row.entry_id, 412);
  assert.equal(row.text, A_CRITIQUE);
});

test("the fourth prompt of a UTC day is refused and writes nothing", async () => {
  const { env, store, statements } = makeEnv();
  const token = await forgeSession("octocat");

  for (let i = 0; i < PROMPTS_PER_DAY; i += 1) {
    const response = await worker.fetch(
      postAuth("/prompt", { prompt: `${A_PROMPT} ${i}` }, token),
      env,
    );
    assert.equal(response.status, 200);
    assert.equal((await response.json()).prompts_left, PROMPTS_PER_DAY - 1 - i);
  }

  const refused = await worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }, token), env);
  assert.equal(refused.status, 429);
  assert.deepEqual(await refused.json(), {
    error: `${PROMPTS_PER_DAY} prompts a day`,
    prompts_left: 0,
  });

  // Refused means refused: the row is not written and then hidden.
  assert.equal(store.submissions.size, PROMPTS_PER_DAY);
  assert.equal(
    statements.filter((s) => s.sql === SQL.insertSubmission).length,
    PROMPTS_PER_DAY,
  );
});

test("the sixth critique of a UTC day is refused and writes nothing", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  for (let i = 0; i < CRITIQUES_PER_DAY; i += 1) {
    const response = await worker.fetch(
      postAuth("/critique", { entry_id: 412, critique: `${A_CRITIQUE} ${i}` }, token),
      env,
    );
    assert.equal(response.status, 200);
    assert.equal((await response.json()).critiques_left, CRITIQUES_PER_DAY - 1 - i);
  }

  const refused = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: A_CRITIQUE }, token),
    env,
  );
  assert.equal(refused.status, 429);
  assert.deepEqual(await refused.json(), {
    error: `${CRITIQUES_PER_DAY} critiques a day`,
    critiques_left: 0,
  });
  assert.equal(store.submissions.size, CRITIQUES_PER_DAY);
});

test("a prompt does not spend the critique budget, nor one login another's", async () => {
  const { env } = makeEnv();
  const octocat = await forgeSession("octocat");
  const hubot = await forgeSession("hubot");

  for (let i = 0; i < PROMPTS_PER_DAY; i += 1) {
    await worker.fetch(postAuth("/prompt", { prompt: `${A_PROMPT} ${i}` }, octocat), env);
  }

  // The prompts are gone; every critique is still there.
  const critique = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: A_CRITIQUE }, octocat),
    env,
  );
  assert.equal(critique.status, 200);
  assert.equal((await critique.json()).critiques_left, CRITIQUES_PER_DAY - 1);

  const me = await (await worker.fetch(get("/me", { bearer: octocat }), env)).json();
  assert.deepEqual(me, {
    username: "octocat",
    prompts_left: 0,
    critiques_left: CRITIQUES_PER_DAY - 1,
  });

  // And the budget is one person's, not the service's.
  const other = await worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }, hubot), env);
  assert.equal(other.status, 200);
  assert.equal((await other.json()).prompts_left, PROMPTS_PER_DAY - 1);
});

test("yesterday's submissions do not count against today", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  // Three prompts from the same login, before the start of the UTC day.
  for (let i = 0; i < PROMPTS_PER_DAY; i += 1) {
    store.submissions.set(900 + i, {
      id: 900 + i,
      kind: "prompt",
      username: "octocat",
      entry_id: null,
      text: A_PROMPT,
      created_utc: "2020-01-01T00:00:01Z",
      updated_utc: "2020-01-01T00:00:01Z",
    });
  }

  const response = await worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }, token), env);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).prompts_left, PROMPTS_PER_DAY - 1);
});

test("an empty prompt or critique is refused in lineage.validate's words", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  for (const value of ["", "   ", "\n\t ", undefined, null, 7]) {
    const prompt = await worker.fetch(postAuth("/prompt", { prompt: value }, token), env);
    assert.equal(prompt.status, 400, `accepted prompt ${JSON.stringify(value)}`);
    assert.deepEqual(await prompt.json(), { error: "the prompt is empty" });

    const critique = await worker.fetch(
      postAuth("/critique", { entry_id: 412, critique: value }, token),
      env,
    );
    assert.equal(critique.status, 400, `accepted critique ${JSON.stringify(value)}`);
    assert.deepEqual(await critique.json(), { error: "the critique is empty" });
  }
  assert.equal(store.submissions.size, 0);
});

test("every code mark is refused, and the error names the mark", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  // One sentence per mark, short, carrying that mark and no other.
  const carrying = {
    "```": "let the lines thin ``` near the edge",
    "{": "let the lines thin { near the edge",
    "}": "let the lines thin } near the edge",
    ";": "let the lines thin ; near the edge",
    "()": "let the lines thin () near the edge",
    "=>": "let the lines thin => near the edge",
    "function ": "let the lines thin function near the edge",
    "<script": "let the lines thin <script near the edge",
    "//": "let the lines thin // near the edge",
    $: "let the lines thin $ near the edge",
  };

  for (const mark of CODE_MARKS) {
    const text = carrying[mark];
    assert.ok(text.includes(mark), `no test text carries ${mark}`);

    const critique = await worker.fetch(
      postAuth("/critique", { entry_id: 412, critique: text }, token),
      env,
    );
    assert.equal(critique.status, 400, `accepted a critique carrying ${mark}`);
    assert.deepEqual(await critique.json(), {
      error: `the critique contains code ('${mark}'); it becomes a prompt, not a patch`,
    });

    const prompt = await worker.fetch(postAuth("/prompt", { prompt: text }, token), env);
    assert.equal(prompt.status, 400, `accepted a prompt carrying ${mark}`);
    assert.deepEqual(await prompt.json(), { error: `the prompt holds code ('${mark}')` });
  }
  assert.equal(store.submissions.size, 0);
});

test("a critique is one sentence, and the count is in the refusal", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  const two = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: "Thin the lines. Soften the edge." }, token),
    env,
  );
  assert.equal(two.status, 400);
  assert.deepEqual(await two.json(), {
    error: "the critique is 2 sentences; one is the contract",
  });

  // A terminator at the end is still one sentence, as in Python.
  const one = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: "Thin the lines near the edge." }, token),
    env,
  );
  assert.equal(one.status, 200);
  assert.equal(store.submissions.size, 1);

  // The one-sentence rule is the critique's. A prompt may describe as much as
  // it likes inside its characters.
  const prompt = await worker.fetch(
    postAuth(
      "/prompt",
      { prompt: "Triangles drift toward the cursor. They thin at the edge." },
      token,
    ),
    env,
  );
  assert.equal(prompt.status, 200);
});

test("a critique of forty words is refused; thirty-nine is not", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  const long = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: words(MAX_CRITIQUE_WORDS) }, token),
    env,
  );
  assert.equal(long.status, 400);
  assert.deepEqual(await long.json(), {
    error:
      `the critique is ${MAX_CRITIQUE_WORDS} words; ` +
      `under ${MAX_CRITIQUE_WORDS} is the contract`,
  });
  assert.equal(store.submissions.size, 0);

  const ok = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: words(MAX_CRITIQUE_WORDS - 1) }, token),
    env,
  );
  assert.equal(ok.status, 200);
});

test("a prompt is capped at its characters; the boundary is allowed", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  const over = "a".repeat(MAX_PROMPT_CHARS + 1);
  const refused = await worker.fetch(postAuth("/prompt", { prompt: over }, token), env);
  assert.equal(refused.status, 400);
  assert.deepEqual(await refused.json(), {
    error:
      `the prompt is ${MAX_PROMPT_CHARS + 1} characters; ` +
      `at most ${MAX_PROMPT_CHARS} is the contract`,
  });
  assert.equal(store.submissions.size, 0);

  const exact = await worker.fetch(
    postAuth("/prompt", { prompt: "a".repeat(MAX_PROMPT_CHARS) }, token),
    env,
  );
  assert.equal(exact.status, 200);

  // The cap is the prompt's: a critique is bounded by its words instead, so a
  // long single word is still fine there.
  const critique = await worker.fetch(
    postAuth("/critique", { entry_id: 412, critique: "a".repeat(MAX_PROMPT_CHARS + 1) }, token),
    env,
  );
  assert.equal(critique.status, 200);
});

test("entry_id must be a positive integer, and that is all this Worker can check", async () => {
  const { env, store } = makeEnv();
  const token = await forgeSession("octocat");

  for (const value of [undefined, null, 0, -1, "12", 1.5, "", true, [412]]) {
    const response = await worker.fetch(
      postAuth("/critique", { entry_id: value, critique: A_CRITIQUE }, token),
      env,
    );
    assert.equal(response.status, 400, `accepted entry_id ${JSON.stringify(value)}`);
    assert.deepEqual(await response.json(), { error: "entry_id must be an entry id" });
  }
  assert.equal(store.submissions.size, 0);

  // An id no entry has is still accepted here: there is no entries table in
  // this database and pretending otherwise would be a lie. The node refuses an
  // unknown parent when it applies the row.
  const unknown = await worker.fetch(
    postAuth("/critique", { entry_id: 999999, critique: A_CRITIQUE }, token),
    env,
  );
  assert.equal(unknown.status, 200);
});

test("/me carries both budgets, and is still 401 signed out", async () => {
  const { env } = makeEnv();
  const token = await forgeSession("octocat");

  assert.equal((await worker.fetch(get("/me"), env)).status, 401);

  const fresh = await worker.fetch(get("/me", { bearer: token }), env);
  assert.equal(fresh.status, 200);
  assert.deepEqual(await fresh.json(), {
    username: "octocat",
    prompts_left: PROMPTS_PER_DAY,
    critiques_left: CRITIQUES_PER_DAY,
  });

  await worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }, token), env);
  await worker.fetch(postAuth("/critique", { entry_id: 412, critique: A_CRITIQUE }, token), env);

  const spent = await worker.fetch(get("/me", { bearer: token }), env);
  assert.deepEqual(await spent.json(), {
    username: "octocat",
    prompts_left: PROMPTS_PER_DAY - 1,
    critiques_left: CRITIQUES_PER_DAY - 1,
  });
});

/** A database that refuses every statement, the way a missing table does. */
function brokenDB() {
  return {
    prepare() {
      return {
        bind() {
          const fail = async () => {
            throw new Error("D1_ERROR: no such table: submissions");
          };
          return { run: fail, all: fail, first: fail };
        },
      };
    },
  };
}

test("/me keeps the session when the budget cannot be counted", async () => {
  // The deploy of 2026-09-16 put the routes live before the table existed, and
  // this is what it cost: /me threw on the COUNT, the gallery read the non-200
  // as signed out, and every signed-in visitor was logged out of a site whose
  // sign-in was working. The name does not depend on the database and must not
  // be lost with it.
  const { env } = makeEnv({ DB: brokenDB() });
  const token = await forgeSession("octocat");

  const response = await worker.fetch(get("/me", { bearer: token }), env);
  assert.equal(response.status, 200);
  // Omitted, not null and not zero: zero would disable the composer's button
  // and claim a spent budget that was never counted.
  assert.deepEqual(await response.json(), { username: "octocat" });

  assert.equal((await worker.fetch(get("/me"), env)).status, 401);
});

test("a budget that cannot be counted still refuses a submission", async () => {
  // The other half of the asymmetry. On /me the count is decoration; here it is
  // the cap, and a cap that cannot be read must not wave the submission
  // through. Neither route may answer 200.
  const { env } = makeEnv({ DB: brokenDB() });
  const token = await forgeSession("octocat");

  await assert.rejects(() =>
    worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }, token), env),
  );
  await assert.rejects(() =>
    worker.fetch(postAuth("/critique", { entry_id: 412, critique: A_CRITIQUE }, token), env),
  );
});

test("/pull delivers submissions on the same inclusive watermark", async () => {
  const { env, store } = makeEnv();
  seedForPull(store);
  store.submissions.set(31, {
    id: 31, kind: "prompt", username: "octocat", entry_id: null,
    text: A_PROMPT,
    created_utc: "2026-09-14T00:00:11Z", updated_utc: "2026-09-14T00:00:11Z",
  });
  store.submissions.set(77, {
    id: 77, kind: "critique", username: "hubot", entry_id: 412,
    text: A_CRITIQUE,
    created_utc: "2026-09-14T00:00:13Z", updated_utc: "2026-09-14T00:00:13Z",
  });

  // A session is not the node's credential here either, submissions or not.
  const token = await forgeSession("octocat");
  assert.equal(
    (await worker.fetch(get("/pull?since=1970-01-01T00:00:00Z", { bearer: token }), env)).status,
    401,
  );

  const all = await worker.fetch(
    get("/pull?since=1970-01-01T00:00:00Z", { bearer: PULL_TOKEN }),
    env,
  );
  const first = await all.json();
  assert.equal(first.submissions.length, 2);
  assert.deepEqual(first.submissions[0], {
    id: 31, kind: "prompt", username: "octocat", entry_id: null,
    text: A_PROMPT,
    created_utc: "2026-09-14T00:00:11Z", updated_utc: "2026-09-14T00:00:11Z",
  });
  // A submission carries the watermark past the last vote, like any other row.
  assert.equal(first.next_since, "2026-09-14T00:00:13Z");

  // Inclusive at both ends: the row sitting on the watermark comes again.
  const again = await worker.fetch(
    get(`/pull?since=${first.next_since}`, { bearer: PULL_TOKEN }),
    env,
  );
  const second = await again.json();
  assert.equal(second.submissions.length, 1);
  assert.equal(second.submissions[0].id, 77);
  assert.deepEqual([second.votes, second.likes, second.views], [[], [], []]);

  // Nothing in a pulled submission identifies a person beyond the username.
  for (const row of first.submissions) {
    assert.deepEqual(
      Object.keys(row).filter(
        (k) => !/^(id|kind|username|entry_id|text|created_utc|updated_utc)$/.test(k),
      ),
      [],
    );
  }
});

test("a submission written through the routes comes back out of /pull", async () => {
  const { env } = makeEnv();
  const token = await forgeSession("octocat");

  const written = await (
    await worker.fetch(postAuth("/prompt", { prompt: A_PROMPT }, token), env)
  ).json();

  const pulled = await (
    await worker.fetch(get("/pull?since=1970-01-01T00:00:00Z", { bearer: PULL_TOKEN }), env)
  ).json();
  assert.equal(pulled.submissions.length, 1);
  assert.equal(pulled.submissions[0].id, written.id);
  assert.equal(pulled.submissions[0].created_utc, written.created_utc);
  assert.equal(pulled.next_since, written.created_utc);
});
