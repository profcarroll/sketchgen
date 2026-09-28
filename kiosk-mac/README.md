# kiosk-mac — the wall's Mac, left alone

What turns a Mac into a gallery wall that comes back by itself: after a reboot, a power cut, a
crash, a hung page or a deploy. The reasons behind every piece are in
[`docs/plans/kiosk-mac.md`](../docs/plans/kiosk-mac.md); this is the kit it planned, made to run.
The first site is the D12 lab (`d12-kiosk` on the tailnet).

## At the Mac

Log in as the account the wall runs as (a standard user; `kiosk` on D12), open Terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/profcarroll/sketchgen/main/kiosk-mac/get.sh | bash
```

`get.sh` fetches this directory into `~/Library/sketchgen-kiosk/kit` and runs `prep.sh`, which:

1. **probes** the Mac, read-only, into `~/kiosk-probe.txt`, and prints the answers that decide
   anything (MDM, FileVault, auto-login, Chrome, Tailscale, Remote Login);
2. asks, then adds `github.com/profcarroll.keys` to the account's `authorized_keys`;
3. asks, then runs **`system.sh`** as root through macOS's own administrator dialog: never sleep,
   boot on power, restart on freeze, no Bluetooth setup assistant, no self-installing macOS
   updates, New York time, Remote Login restricted to keys, the tailnet and this one account,
   and a sudo rule for exactly `shutdown -r now`;
4. starts Tailscale in this account;
5. prints what only a person can click in System Settings: Tailscale at login, FileVault off,
   auto-login, lock screen, Do Not Disturb.

## From the laptop, once the Mac is on the tailnet

```bash
ssh d12-kiosk 'bash ~/Library/sketchgen-kiosk/kit/install.sh'
```

`install.sh` needs no administrator. It installs `launch-kiosk.sh` and the watchdog under
`~/Library/sketchgen-kiosk/`, `kiosk-status` in `~/bin`, and the two LaunchAgents; sets the
account's own settings (no screen saver, no window restore, no crash dialogs, no hot corners);
quits any Chrome started by hand; and waits until the page's title shows up.
`install.sh --remove` takes the agents out again.

| file | runs | does |
| --- | --- | --- |
| `launch-kiosk.sh` | by `org.sketchgen.kiosk`, kept alive | waits for the gallery, clears Chrome's crash flag, parks the pointer, execs Chrome `--kiosk` on `kiosk.html?unattended=1&site=d12` |
| `kiosk-watchdog.sh` | by `org.sketchgen.kiosk-watchdog`, every 5 min | restarts Chrome when the page's title has not changed in 20 min or the page is gone, and at 04:30 |
| `kiosk-status` | `ssh d12-kiosk kiosk-status` | uptime, the agent, Chrome's memory, the title, the URL, the local gallery if any, Tailscale, the last watchdog and launcher lines |
| `serve-gallery.sh`, `serve-gallery.py` | by `org.sketchgen.gallery`, kept alive, only after `install.sh --local` | serves `~/Library/sketchgen-kiosk/gallery` on 127.0.0.1:8090, loopback only, no access log |
| `sync-local.sh` | on the **laptop** | copies a `render-local` tree from a node to the wall as a new release and swaps it in |

## A wall that plays its own copy

A wall can play a local render of the gallery — a held archive, or the site's own entries —
from its own loopback instead of from Pages (`docs/plans/local-gallery.md`). It needs the
Command Line Tools, because the server is their `python3`; without them `/usr/bin/python3` is
a stub that puts an installer dialog over the wall, so `install.sh --local` refuses.

```bash
# once, at the Mac if the tools are missing:  xcode-select --install
ssh WALL 'bash ~/Library/sketchgen-kiosk/kit/install.sh --local'
# on the node: the render (see docs/OPERATIONS.md → A local render)
bin/sg render-local --db … --jobs … --out ~/sketchgen-local/NAME --origin NAME --include … \
    --config-from ~/sketchgen/gallery --write-path https://sketchgen-writepath.sketchgen.workers.dev
# on the laptop: onto the wall, as a new release
kiosk-mac/sync-local.sh sld-cloud:sketchgen-local/NAME WALL
# point the wall at it, and restart the page
ssh WALL 'echo "http://127.0.0.1:8090/kiosk.html?unattended=1&site=ROOM" > ~/Library/sketchgen-kiosk/url \
    && launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk'
```

`--write-path` lets the wall count views: the Worker accepts them from `127.0.0.1:8090` (its
`KIOSK_ORIGINS`), anonymously and for kiosk views only. The room in `site=` must be in the
gallery's `config.json` `kiosk_sites`, which `--config-from` carries over, or the kiosk counts
by the attendance rule. Back to Pages: `rm ~/Library/sketchgen-kiosk/url` and the same
kickstart. `install.sh --remove-local` stops the server and leaves the releases.

## Afterwards

The D12 Mac's particulars, how to reach its screen, and the traps met setting it up are in
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md) → *The D12 kiosk Mac*.

| to | do |
| --- | --- |
| see it | `ssh d12-kiosk kiosk-status` |
| restart the page | `ssh d12-kiosk 'launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk'` |
| restart the Mac | `ssh d12-kiosk 'sudo shutdown -r now'` |
| change the URL or a flag | write it to `~/Library/sketchgen-kiosk/url` over ssh (or edit `launch-kiosk.sh`, whose default it overrides), then restart the page |
| put a new local render on it | `kiosk-mac/sync-local.sh NODE:DIR WALL` from the laptop; the page picks it up within 15 min |
| update the kit | run the `curl … get.sh` line again at the Mac, or `install.sh` after copying files over |
