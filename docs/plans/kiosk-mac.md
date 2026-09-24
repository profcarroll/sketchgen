# Kiosk: the D12 Mac mini, left alone

The kiosk has been on a mounted display in the D12 lab since 2026-09-23, driven by an Intel Mac
mini with 8 GB. It was set up by hand for a first look. This document is what it takes to walk
away from it: a box that comes back by itself after a reboot or a power cut, picks up every
deploy without anyone touching it, counts views by the building's hours rather than by who
touched the mouse, and can be looked at and fixed from the laptop.

Two halves, because they happen in two places:

- **Packet A, `feat/kiosk-unattended`** — changes to `kiosk.js` and the manifest. Built and
  deployed from the laptop **before** the on-device session, so that the session only has to
  point a browser at a page that already knows how to run unattended.
- **The session** — on the Mac, with the operator in the room: probe, configure, then break it on
  purpose and watch it recover. The draft scripts are in `docs/plans/kiosk-mac/`.

## 0. What the code does today, and why it will not survive a reboot

Read against `sketchgen/assets/kiosk.js` at 89d633b. Each one is a gap between *a projector
somebody started* and *a projector nobody will touch again*.

| # | gap | where | what the wall shows |
| --- | --- | --- | --- |
| 1 | The start card waits for a click | `wireStart` | after a reboot, a *Start* button, forever |
| 2 | A manifest that fails to load is final | `cannotStart` | at boot, before Wi-Fi is up: *Could not load…*, forever |
| 3 | `kiosk.json` is read once | `loadManifest`, only in `ready` | the gallery as it was the morning it was switched on; a `kiosk.js` fix never arrives |
| 4 | The cursor hides only after the mouse **moves**, and only over the page | `wireIdle`, `body.kiosk.idle` | an arrow wherever the pointer was left at login. Over the sketch it is the frame's cursor: an opaque sandboxed frame is not the page's to style, and its mouse moves never reach the page |
| 5 | Counting stops after 8 h with no key or mouse | `countingViews`, `VIEW_STOP_S` | a kiosk nobody touches stops counting at about 5 pm on its first day and never starts again |

Gap 5 is the one the operator asked about: the attendance rule in `kiosk-views.md` §3.2 was
written for a projector somebody walks up to. A wall in a lab that nobody touches is the case it
gets exactly wrong — it cannot tell an empty room at 3 am from a full one at 3 pm, because in
both nobody touched the keyboard.

## 1. Packet A — `feat/kiosk-unattended`

Generator only. No migration, no Worker deploy, no D1 change: `/view` already takes
`source: "kiosk"` and D1 already keeps `kiosk_count` (`writepath/worker.js:712`).

### 1.1 `?unattended=1`

One parameter, carried by `query()` like `views=0` and never persisted: a keyboard must not be
able to turn it on or off. It means *nobody is going to click anything*:

- **No card.** `play()` runs as soon as the manifest is in hand. Full screen comes from the
  browser (`--kiosk`, §2.3), not from `requestFullscreen`, which needs the gesture nobody gives;
  audio likewise comes from `--autoplay-policy=no-user-gesture-required`.
- **Retry, never give up.** A failed manifest schedules another try — 10 s, doubling to 5 min —
  instead of `cannotStart()`. The card copy is unchanged for an attended kiosk.
- **The cursor is gone from the first frame.** `body.idle` is set in `play()`, not only after a
  mouse move, and `body.kiosk.unattended .stage iframe.sketch { pointer-events: none }` puts the
  whole stage under the page's `cursor: none` and hands every mouse move to `wireIdle`. The ghost
  pointer is unaffected: it is dispatched *inside* the frame by its own shim. What is given up is
  a visitor clicking a sketch with a real mouse — on a wall with no mouse, nothing.

### 1.2 Staying current

- **`build` in `kiosk.json`**: a short hash over the bytes of `assets/kiosk.js`,
  `templates/kiosk.html` and `assets/gallery.css`, written by the manifest writer beside
  `entries`. Deterministic, so `render-index` over unchanged code leaves it unchanged.
- **Every 15 minutes** (`?refresh=` in seconds overrides, for the session's test) the page
  re-reads `config.json` and `kiosk.json` with `cache: "no-cache"`, so GitHub Pages answers an
  unchanged 600 KB manifest with a 304 on its ETag.
  - `build` changed → `location.reload()` at the next seat boundary (between sketches, behind the
    fade), with the same query string. `?unattended=1` brings it straight back.
  - entries changed → swap `ENTRIES` and rebuild `seq` at the next seat boundary; new entries
    play without a reload.
  - `config.json` changed → take it: `kiosk_views`, `kiosk_sites` and `kiosk_buildings` apply
    from the next seat.
- Unattended only. An attended kiosk keeps today's behaviour; a reload under somebody pressing
  keys would be rude.

### 1.3 Views by the building's hours, for a kiosk that knows where it is

**A default kiosk does not know where it lives.** `kiosk.html`, with or without
`?unattended=1`, counts exactly as it does today: the 10-second dwell and the 8-hour attendance
rule. **A kiosk is made site-specific by its URL**: `?site=d12`, carried by `query()` like
`views=0` and never persisted, so the site is programmed where the machine is programmed (its
launcher, §2.4), and a bumped keyboard cannot move a kiosk to another building. D12 is the case
study and the first site.

The sites and the buildings they are in live in the gallery's `config.json`, as two new keys
read by `Config` like `kiosk_views` (dataclass, `load`, `to_json`), both absent by default:

- `kiosk_sites` — site id → `{ "name", "building" }`. A room.
- `kiosk_buildings` — building id → `{ "name", "tz", "source", "terms", "exceptions" }`. The
  hours belong to the building, not the room, so a second kiosk in the Vera List Center is one
  line in `kiosk_sites` and shares the schedule — one place to extend it each term.

```json
"kiosk_sites": {
  "d12": { "name": "D12 lab", "building": "vera-list" }
},
"kiosk_buildings": {
  "vera-list": { …the block below… }
}
```

The D12 lab is in the **Vera List Center** (6 East 16th Street / 79 Fifth Avenue). The New School
posts its hours by term, with closures and 24/7 finals periods on top
(<https://www.newschool.edu/about/campus-information/building-hours/>, read 2026-09-23). A single
weekly table cannot say that, so a building is terms plus dated exceptions:

```json
"vera-list": {
  "name": "Vera List Center, 6 East 16th Street / 79 Fifth Avenue",
  "tz": "America/New_York",
  "source": "https://www.newschool.edu/about/campus-information/building-hours/",
  "terms": [
    { "name": "Fall 2026", "from": "2026-08-17", "to": "2026-12-18",
      "week": { "mon": "07:30-24:00", "tue": "07:30-24:00", "wed": "07:30-24:00",
                "thu": "07:30-24:00", "fri": "07:30-24:00", "sat": "07:30-24:00",
                "sun": "10:00-24:00" } },
    { "name": "Winter 2026-27", "from": "2026-12-19", "to": "2027-01-18",
      "week": { "mon": "07:30-20:00", "tue": "07:30-20:00", "wed": "07:30-20:00",
                "thu": "07:30-20:00", "fri": "07:30-20:00", "sat": "07:30-20:00",
                "sun": "10:00-20:00" } },
    { "name": "Spring 2027", "from": "2027-01-19", "to": "2027-05-14",
      "week": { "mon": "07:30-24:00", "tue": "07:30-24:00", "wed": "07:30-24:00",
                "thu": "07:30-24:00", "fri": "07:30-24:00", "sat": "07:30-24:00",
                "sun": "10:00-24:00" } }
  ],
  "exceptions": [
    { "from": "2026-11-25", "to": "2026-11-29", "hours": null,          "why": "Thanksgiving" },
    { "from": "2026-11-30", "to": "2026-11-30", "hours": "07:30-24:00", "why": "24/7 begins" },
    { "from": "2026-12-01", "to": "2026-12-17", "hours": "00:00-24:00", "why": "24/7 finals" },
    { "from": "2026-12-18", "to": "2026-12-18", "hours": "00:00-24:00", "why": "24/7 ends at the usual close" },
    { "from": "2026-12-24", "to": "2027-01-03", "hours": null,          "why": "Winter break" },
    { "from": "2027-01-18", "to": "2027-01-18", "hours": null,          "why": "MLK Day" },
    { "from": "2027-02-15", "to": "2027-02-15", "hours": null,          "why": "Presidents' Day" },
    { "from": "2027-04-26", "to": "2027-05-14", "hours": "00:00-24:00", "why": "24/7 finals" }
  ]
}
```

The rule for one instant, in `tz`: the first `exceptions` range that contains today decides
(`null` is closed); otherwise the term that contains today, by weekday; **a date in no term is
closed.** That last clause is the maintenance contract: the posted schedule ends on 2027-05-14,
and summer 2027 is not published yet. When the list runs out the wall keeps playing and stops
counting, and the title says `not counting: no posted hours` so `kiosk-status` shows it — a
schedule nobody extended must under-count, never over-count. Extending it is one edit to the
gallery's `config.json` a term, when the university posts the next one.

`24:00` is the midnight close, and a span is within one calendar day: the posted hours never
cross midnight except during 24/7, which is whole days.

Two things on the university's page to know about, not to fix: Labor Day weekend (5–7 September)
has passed and is left out; and the Winter heading says *December 19, 2025 to January 18, 2026*
while its closures are 2026–27 and Spring starts 19 January 2027 — the year in that heading is
stale, and the block above uses 2026–27.

`countingViews()` becomes: no write path, `?views=0` or `kiosk_views: false` → no, as now; then
**with `?site=`, count exactly when the site's building is open by the rule above**, and the
attendance rule is not consulted; **without it, the 8-hour rule, unchanged.** The 10-second dwell and once-per-seat guard stand in both cases — they are what stop a
per-frame bug, not what decides whether anyone is there.

- The clock is `Intl.DateTimeFormat(…, { timeZone: tz, hourCycle: "h23" }).formatToParts`,
  so a Mac whose own time zone is wrong still counts New York's hours.
- **Fail closed, and say so.** A `?site=` that is not in `kiosk_sites`, a site whose building is
  missing, or a building block that does not parse counts nothing, and the title names which
  (`not counting: unknown site d12x`). A kiosk that asked to be site-specific must never fall
  back to the attendance rule silently — that would be counting on a rule its operator opted out
  of.
- Outside hours a person at the keyboard still does not count. The premise is that the building
  is the audience, and the rule should be one sentence long.
- The menu names the site, and the footer copy gains the rule: *D12 lab · counts a view after
  ten seconds on screen, during Vera List Center hours.* A default kiosk's footer is unchanged.
- **The site is not sent with the view** in this packet: `/view` still carries only
  `source: "kiosk"`, so D1 knows a view came from *a* projector, not which. See §4, decision 7 —
  it is the one choice here that cannot be made later for the views already counted.

### 1.4 The title is the status line

`document.title` is kept current: `sketchgen kiosk · d12 · #1234 · counting · b3f9a1` (or
`not counting: closed`, `not counting: unknown site d12x`, or `waiting for the gallery (try 7)`;
a default kiosk shows no site). Nothing is written anywhere — a
title is not a write — but Chrome's DevTools endpoint on the Mac lists every tab's title, so the
watchdog (§2.4) and `kiosk-status` read the page's state with one `curl` and no browser
automation. A title that has not changed in twenty minutes is a hung renderer; `every` is at most
600 s, so a healthy one always changes within ten.

### 1.5 Tests

Against `tests/js/kiosk.js`, which already steps timers and rAF by hand:

- unattended: plays with no click; a failed manifest retries on the backoff and plays when it
  succeeds; `body.idle` from the first frame; frame has `pointer-events: none`;
- refresh: a new `build` reloads once, at a seat boundary, never mid-sketch; new entries join
  without a reload; an unchanged 304 does nothing; attended pages never refresh;
- hours, against the real block above: 07:29 on a fall Monday does not count and 07:30 does;
  23:59 counts and 00:00 does not; a winter Tuesday at 20:30 does not; Thanksgiving Thursday
  does not; 03:00 on 2026-12-08 does (24/7); 2027-05-15 does not (no term); an exception beats
  its term; a malformed block counts nothing; a kiosk with no `?site=` keeps the 8-hour rule
  even when `kiosk_sites` is set; an unknown site counts nothing; `site` survives a key press
  in the address bar (`query()`) and is never written to storage;
  absent falls back to the 8 h rule (the existing tests, unchanged); the time zone is the
  config's and not the machine's (stub `Intl` with a fixed instant);
- the 600-frame one-view test runs again under hours;
- `test_it_writes_one_thing_and_only_one` and `test_it_presents_no_identity` unchanged — this
  packet adds reads, never a write;
- `Config` round-trips `kiosk_sites` and `kiosk_buildings`; `build` is stable across two renders of unchanged code
  and moves when `kiosk.js` does.

### 1.6 Deploy

Merge → `ssh sld-cloud 'bash ~/sketchgen/app/update.sh'`, and **not** with `--no-render`: the
packet touches `gallery.py`, a template and an asset, and `render-index` has to write the new
`kiosk.json`, `kiosk.html`, `assets/kiosk.js` and `config.json`. Pull the gallery checkout on
the node first if the PR was merged from the laptop. Then add
`kiosk_sites` and `kiosk_buildings` to the gallery checkout's `config.json` and run
`render-index` once more (or let the next publish carry it). Verify on the live site with
`?unattended=1&site=d12&refresh=60`.

## 2. The session on the Mac

Target: one sitting, about two hours, most of it waiting on reboots. The order matters —
Tailscale goes on **before** anything that could lock the operator out, so every later step can
be undone from the laptop.

### 2.1 Probe (read-only, first ten minutes)

`bash probe.sh | tee ~/kiosk-probe.txt` prints all of it; the list is what to read in the output
and what each answer decides.

| check | command | decides |
| --- | --- | --- |
| model, year, CPU, RAM | `system_profiler SPHardwareDataType` | Macmini8,1 (2018) supports macOS 15; older caps the OS |
| macOS version | `sw_vers` | which `pmset`/`softwareupdate` flags exist |
| **managed by New School IT?** | `profiles status -type enrollment` | if MDM-enrolled: ask IT before Tailscale, auto-login, or turning off updates. Stop and decide here |
| FileVault | `fdesetup status` | **on blocks auto-login.** Off is the price of coming back unattended |
| accounts, which is admin | `dscl . list /Users \| grep -v ^_`, `id` | a dedicated standard `kiosk` user, or the current one |
| auto-login | `defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser` | |
| power | `pmset -g`, `pmset -g custom` | `sleep`, `displaysleep`, `autorestart`, `womp` |
| schedule | `pmset -g sched` | any existing wake/sleep schedule |
| network | `networksetup -listallhardwareports`, `route get default`, `scutil --nwi` | Ethernet or Wi-Fi; **Wi-Fi on a personal 802.1X login ties the wall to a password that expires** |
| captive portal / proxy | `curl -sI https://profcarroll.github.io/sketchgen-gallery/kiosk.json`, `scutil --proxy` | can it reach Pages and the Worker without a login page |
| time | `systemsetup -gettimezone`, `sntp -d time.apple.com` (or `sntp time.apple.com`) | NTP on; the hours rule uses the config's zone but logs use the Mac's |
| display | `system_profiler SPDisplaysDataType` | resolution, refresh, the GPU (Intel UHD 630 expected) |
| browser | `ls /Applications`, Chrome version | Chrome, and whether it is the one currently showing the kiosk |
| how it is launched now | `launchctl list \| grep -v com.apple`, Login Items | what to remove so there are not two |
| Homebrew / CLT | `which brew`, `xcode-select -p` | Tailscale route (§2.2) |
| disk | `df -h /` | at least 10 GB free for updates |
| memory pressure | `memory_pressure`, `vm_stat` | baseline before Chrome runs a day |
| input devices | `system_profiler SPBluetoothDataType SPUSBDataType` | is a keyboard/mouse attached; if not, the Bluetooth setup assistant will appear at boot |
| updates | `softwareupdate -l`, `defaults read /Library/Preferences/com.apple.SoftwareUpdate` | install anything pending **now**, while someone is in the room |
| Tailscale already? | `which tailscale`, `/Applications/Tailscale.app` | |
| firewall | `/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate` | leave it on; Tailscale SSH does not need a hole |

Also, by eye and by asking: does the **display** have its own auto-off, eco or "no signal →
standby" setting (a TV that goes to standby on a signal blip may not wake); is the outlet on a
switched or timed circuit; is the keyboard reachable by passers-by; is audio wanted, and at what
volume; is anything in the lab's rules about devices on the network.

### 2.2 Tailscale first

Recommended: the open-source `tailscaled` as a **system daemon**, because it runs at boot before
anyone logs in, and it is the macOS variant that can be a Tailscale SSH server.

```bash
brew install tailscale
sudo tailscaled install-system-daemon
sudo tailscale up --ssh --hostname=d12-kiosk      # prints a login URL; open it on the laptop
```

Then, in the admin console: **disable key expiry** on `d12-kiosk` (otherwise it drops off the
tailnet in 180 days, silently), tag it `tag:kiosk`, and make sure the tailnet policy has an
`ssh` rule that lets the operator's devices in as the Mac's users. Test from the laptop before
going on: `ssh <user>@d12-kiosk 'uptime'`.

If Homebrew is not wanted on the box: the standalone Tailscale app from tailscale.com runs at
login (fine with auto-login) but cannot be a Tailscale SSH server — then turn on macOS **Remote
Login** and reach `sshd` over the tailnet address instead. Either way, also turn on **Screen
Sharing** for the one time a picture is worth more than a `curl` (`vnc://d12-kiosk` from the
laptop, over the tailnet only).

If the probe says the Mac is MDM-managed, this is the step to clear with IT first.

### 2.3 macOS, set for a wall

Each line is reversible, and each is there because of what happens without it.

| setting | how | without it |
| --- | --- | --- |
| never sleep | `sudo pmset -a sleep 0 displaysleep 0 disksleep 0 powernap 0` | black wall at 10 min |
| power back → boot | `sudo pmset -a autorestart 1` and `sudo systemsetup -setrestartfreeze on` | a power cut is the end of the day |
| auto-login | System Settings → Users & Groups → Automatically log in as (FileVault off) | a login window after every reboot |
| no screen saver, no lock | `defaults -currentHost write com.apple.screensaver idleTime 0`; Lock Screen → never | |
| no Bluetooth setup assistant | `sudo defaults write /Library/Preferences/com.apple.Bluetooth BluetoothAutoSeekKeyboard -bool false` and `… BluetoothAutoSeekPointingDevice -bool false` | a "no keyboard found" window over the kiosk at every boot without one |
| don't reopen windows | `defaults write com.apple.loginwindow TALLogoutSavesState -bool false` | a second, stray Chrome window |
| no crash dialogs | `defaults write com.apple.CrashReporter DialogType none` | a dialog over the wall that nobody dismisses |
| updates: download, don't install | `sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false` | a surprise restart into an update screen |
| notifications | Focus → Do Not Disturb, always on | banners over the wall |
| hot corners | Desktop & Dock → Hot Corners → all off | a parked cursor in a corner starts the screen saver |
| volume | `osascript -e 'set volume output volume N'` | |
| time | automatic date & time on, zone New York | |

### 2.4 The browser, supervised

Chrome, because Safari has no kiosk mode and Chrome's `--kiosk` has no chrome, no menu bar and
no dock. Three draft files in `docs/plans/kiosk-mac/`, installed under `~/Library/`:

- **`launch-kiosk.sh`** — waits until the gallery answers (so Chrome never boots into its own
  offline page), clears Chrome's "crashed" flag so there is no *Restore pages?* bubble, nudges the
  pointer into a corner, and `exec`s Chrome with a profile of its own:
  `--kiosk --no-first-run --no-default-browser-check --noerrdialogs
  --autoplay-policy=no-user-gesture-required --disable-features=Translate
  --remote-debugging-port=9222` (loopback only; Chrome refuses a debugging port on the default
  profile, which is one more reason for the dedicated one) at
  `…/kiosk.html?unattended=1&site=d12` — this line is where the machine learns where it lives.
- **`org.sketchgen.kiosk.plist`** — a LaunchAgent: `RunAtLoad`, `KeepAlive`,
  `ThrottleInterval 10`. Chrome quits, crashes, or someone presses ⌘Q → it is back in ten
  seconds. This replaces any Login Item or "open at login" the first setup used.
- **`org.sketchgen.kiosk-watchdog.plist` + `kiosk-watchdog.sh`** — every 5 minutes, reads the
  tab title from `http://127.0.0.1:9222/json/list` (§1.4). No answer, or the same title for 20
  minutes → `launchctl kickstart -k` the kiosk agent. At 04:30 it restarts Chrome regardless,
  which picks up Chrome's own updates and gives back a day's memory on an 8 GB machine.
- **`kiosk-status`** — what the laptop runs over ssh: uptime, the agent's state, Chrome's
  resident memory, the title, the last watchdog lines.

Install, as the auto-login user (after removing whatever launches the browser today):

```bash
D=~/Library/sketchgen-kiosk; mkdir -p $D ~/Library/Caches/sketchgen-kiosk ~/bin
cp launch-kiosk.sh kiosk-watchdog.sh $D/ && cp kiosk-status ~/bin/
for p in org.sketchgen.kiosk org.sketchgen.kiosk-watchdog; do
  sed "s/USER/$(whoami)/g" $p.plist > ~/Library/LaunchAgents/$p.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$p.plist
done
```

**The watchdog goes on only once Packet A is live.** Before it, the page's title never
changes, and the watchdog would restart a healthy wall every twenty minutes. For the same
reason, a visitor who pauses the wall with the space bar gets it restarted twenty minutes later
— which, on an unattended wall, is the right answer.

The cursor, finally, has three layers and needs all three: the page hides it (§1.1); the
launcher parks it in the bottom-right corner (Chrome sometimes does not repaint a `cursor: none`
until the pointer moves, so the launcher moves it); and the mouse, if there is one, goes in a
drawer. If the probe shows no mouse at all, the page's rule alone should do — the session checks.

### 2.5 Break it on purpose

Nothing counts as done until it has recovered from each of these with nobody touching it. Note
the time from fault to a sketch playing; each should be under three minutes.

1. **Reboot.** `sudo shutdown -r now` from the laptop over Tailscale. Wall comes back playing, no
   cursor, no dialog, no Start card.
2. **Pull the power.** At the wall, count to ten, plug in. Same.
3. **Kill the browser.** `killall "Google Chrome"`; and ⌘Q at the keyboard. Back in ten seconds.
4. **Network at boot.** Unplug Ethernet (or turn Wi-Fi off), reboot, wait two minutes, restore.
   Plays within a minute of the network returning.
5. **Hung page.** `kill -STOP` the renderer for the kiosk tab; the watchdog restarts Chrome
   within 25 minutes (or run `kiosk-watchdog.sh` by hand with a short threshold).
6. **A deploy arrives.** With `&refresh=60` on the URL for this test only: run `render-index`
   on the node; within two minutes the title's build hash changes (or, if the code did not
   change, the new entry is in rotation). Take `refresh` back off.
7. **Views.** With the title saying `counting`, watch one entry's count move on `/counts`;
   temporarily set the Vera List Center's hours for today to a span that has ended and see the title say `not counting:
   closed` after the next refresh.
8. **From the laptop, the whole remote kit**: `ssh d12-kiosk kiosk-status`, restart the kiosk,
   restart the Mac, open Screen Sharing once.

Then leave it overnight and read `kiosk-status` from the laptop the next morning: uptime,
Chrome memory after the 04:30 restart, and that the title moved through the night.

### 2.6 What goes back into the repository

The drafts in `docs/plans/kiosk-mac/` become whatever the session found they had to be, and the
operations notes get a short *D12 kiosk* section in `docs/OPERATIONS.md`: hostname, how to reach
it, the four remote commands, and the Mac's settings that differ from stock.

## 3. Minimal management, afterwards

| to | do |
| --- | --- |
| see it | `ssh d12-kiosk kiosk-status` |
| restart the page | `ssh d12-kiosk 'launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk'` |
| restart the Mac | `ssh d12-kiosk 'sudo shutdown -r now'` |
| extend or change hours | edit the building in `kiosk_buildings` in the gallery's `config.json`, `render-index`; every kiosk in that building takes it within 15 min |
| stop counting everywhere | `kiosk_views: false`, same path |
| move or add a kiosk | a line in `kiosk_sites` if the room is new; `site=` in that Mac's launcher |
| change the URL or flags | edit `~/Library/sketchgen-kiosk/launch-kiosk.sh` over ssh, restart the page |
| macOS updates | monthly, over ssh: `softwareupdate -l`, then install with the reboot, then `kiosk-status` |

Nothing about ordinary gallery work touches the Mac: publishing an entry, or deploying a
`kiosk.js` fix, reaches the wall by itself (§1.2).

## 4. Decisions for the operator

1. **Building hours, not the lab's.** §1.3 counts by the Vera List Center's posted hours
   (settled 2026-09-23). If D12 itself locks earlier than the building, say so and the spans
   narrow; nothing else changes.
2. **FileVault off** on this Mac, for auto-login. Recommended — the wall shows only what is
   public, and the alternative is a login window after every power cut.
3. **A dedicated `kiosk` standard user**, or the current account. Recommended: dedicated, with
   the admin account kept separate, so a passer-by at the keyboard has nothing but the wall.
4. **Homebrew `tailscaled` with Tailscale SSH** (recommended), or the standalone app plus macOS
   Remote Login.
5. **Outside hours: keep playing, or go dark?** Recommended: keep playing and simply not count.
   Going dark (`pmset repeat` and a black page) saves the display but adds a wake path that is one
   more thing to fail on a morning nobody is watching. Revisit if the display has burn-in risk.
6. **Audio**: on, at what volume, or muted.
7. **Tag views with the site?** Recommended **yes, in Packet A**, for the reason
   `kiosk-views.md` §3.4 gave for keeping `kiosk_count`: a split not recorded now is gone for
   every view counted before it. It is a D1 table `kiosk_views (entry_id, site, count,
   updated_utc)` bumped beside `views.kiosk_count`, `/view` accepting an optional `site` matching
   `^[a-z0-9-]{1,32}$`, and nothing downstream reading it yet — so it is a Worker deploy with a
   D1 change first (AGENTS.md → *Deploying*). The site is a room, not a person, so it keeps the
   kiosk's no-identity rule. No means Packet A stays generator-only.
