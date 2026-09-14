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
  };
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
        const [entryId, updated] = args;
        const existing = store.views.get(entryId);
        store.views.set(entryId, {
          entry_id: entryId,
          count: existing ? existing.count + 1 : 1,
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
              execute(sql, args);
              return { success: true };
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
    assert.equal(callback.headers.get("Location"), GALLERY_URL);
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

    // /me now answers with the username and nothing else.
    const me = await worker.fetch(get("/me", { cookie: session }), env);
    assert.equal(me.status, 200);
    assert.deepEqual(await me.json(), { username: "octocat" });
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
