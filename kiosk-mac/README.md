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
| `kiosk-status` | `ssh d12-kiosk kiosk-status` | uptime, the agent, Chrome's memory, the title, Tailscale, the last watchdog and launcher lines |

## Afterwards

The D12 Mac's particulars, how to reach its screen, and the traps met setting it up are in
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md) → *The D12 kiosk Mac*.

| to | do |
| --- | --- |
| see it | `ssh d12-kiosk kiosk-status` |
| restart the page | `ssh d12-kiosk 'launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk'` |
| restart the Mac | `ssh d12-kiosk 'sudo shutdown -r now'` |
| change the URL or a flag | edit `~/Library/sketchgen-kiosk/launch-kiosk.sh` over ssh, then restart the page |
| update the kit | run the `curl … get.sh` line again at the Mac, or `install.sh` after copying files over |
