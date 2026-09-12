# Claude Code Remote

Type a prompt on your phone. It runs as a real Claude Code session, in the
folder you picked, on your own server. Replies stream back into the same thread,
and follow-up messages continue that session with its full memory.

```
phone (PWA, added to home screen)
   |  https, over Tailscale only
   v
orchestrator (FastAPI, systemd, binds 127.0.0.1)
   |  claude -p --session-id <uuid> --output-format stream-json   (turn 1)
   |  claude -p --resume     <uuid> --output-format stream-json   (turn 2..n)
   v
your folders on the VPS
```

The prompt is handed to Claude Code **on stdin, byte for byte** — never through
argv — so quotes, newlines and length never reshape what you typed.

---

## Deploy

**Server:** Contabo **Cloud VPS 4** (4 vCPU / 8 GB / 100 GB SSD, ~$5.28/mo),
EU/Nuremberg, **Ubuntu 24.04**. Hetzner's equivalent tier costs ~$14/mo for a
quarter of the machine; this was measured, not assumed.

`bootstrap.sh` has been executed end to end on real Ubuntu 24.04 — it is not
a script that has only ever been syntax-checked.

```bash
scp bootstrap.sh root@<ip>:                 # 1. copy it up
ssh root@<ip> 'bash bootstrap.sh'           # 2. run it (add --with-ntfy for push)
./scripts/sync.sh claude@<ip>               # 3. push the orchestrator
ssh root@<ip> 'bash bootstrap.sh'           # 4. re-run to finish the install
```

Then four things the script cannot do for you. `bootstrap.sh` prints them all
at the end; the two that are easy to get wrong:

**Tailscale.** Never pass `--accept-dns=false` (it breaks the box's own DNS),
and `tailscale serve` takes 1-2 minutes on first run while it provisions a
certificate — it is not hung. Afterwards, **disable key expiry** at
<https://login.tailscale.com/admin/machines>, or the node silently drops off
the tailnet in ~180 days, while you are away from your computer.

**Claude Code auth.** `claude setup-token` does *not* store a login on disk.
It prints a `sk-ant-oat01-...` token, and that token in the environment IS the
authentication:

```bash
sudo -iu claude          # separate, interactive commands - piping these
claude setup-token       # through ssh as a one-liner silently does nothing
# then, as root:
echo 'CLAUDE_CODE_OAUTH_TOKEN=<paste>' >> /etc/ccremote.env
systemctl restart ccremote
```

Then check the whole thing:

```bash
bash /opt/ccremote/scripts/verify.sh
```

It checks boot persistence, Tailscale, key expiry, the token, swap, API auth,
and whether your project folders actually exist on that machine.

Open that `https://…ts.net` URL on your phone, paste the token bootstrap printed,
then **Add to Home Screen**. It opens fullscreen with no browser chrome.

> Use Tailscale Serve rather than binding to the raw tailnet IP. A `http://100.x.x.x`
> page is not a secure context, so the service worker and installable-PWA behaviour
> are both unavailable there. The `ts.net` name gets a real certificate.

## Folders

There is no list of projects to maintain. `tasks.yaml` names one workspace root:

```yaml
workspace:
  root: ~/projects
  default_profile: default
```

Every folder under it is reachable from the phone, and the phone can create new
ones. Making a folder is what adds a project; nothing has to be declared first.

The root is the only boundary, and it is enforced by resolving the requested
path — **symlinks included** — before anything runs. `..`, an absolute path
elsewhere, and a symlink planted inside the workspace all resolve outside and are
refused; a symlink that escapes is also left out of the listing, so it can't be
tapped. That is what keeps `/etc`, another service's `.env`, and the rest of the
disk out of reach without an inventory of what to exclude.

`projects:` still exists, but only as **shortcuts**: a friendlier name and a
default profile, pinned to the top of the picker. Removing an entry does not make
its folder unreachable — you just browse to it instead.

Bringing work onto the server happens from the phone too, for **any** repository
rather than a registered few:

```
sudo ccr-git list                    every repo the configured tokens can see
sudo ccr-git clone owner/name [dir]  clones into the workspace
sudo ccr-git pull <dir>              updates it
```

The obvious alternative — a token in the session user's `~/.git-credentials` —
would let any `full` session read it and push anywhere you can. So instead
`ccr-git` runs git as root and hands the token to it through `GIT_ASKPASS`,
never on a command line: `/proc/<pid>/cmdline` is world-readable on Linux, so a
token inside a clone URL is visible to `ps`. The session may invoke the helper
through a single `NOPASSWD` sudoers entry and nothing else.

Arguments are validated as `owner/name` plus a plain folder name, so no git
option can be smuggled in — `--upload-pack=<command>` would otherwise be remote
code execution as root.

A fine-grained GitHub token belongs to exactly one resource owner, so tokens are
per owner:

```
/etc/ccremote-git.d/<owner>.token   root 600, e.g. alice.token
/etc/ccremote-git.token             fallback for owners with no file
```

After editing `tasks.yaml`:

```bash
curl -XPOST -H "X-Token: $CCR_TOKEN" localhost:8080/api/reload
```

---

## Uncommitted work (mac/ccr-autosave)

The server can only run on code that is on the server, so anything you have not
pushed is invisible to it -- including a whole feature branch and 41 modified
files, which is what one of these repos actually had.

`mac/ccr-autosave` closes that gap. Every few minutes it snapshots each
configured worktree into a commit built in a **throwaway index**, and pushes it
over SSH to `refs/heads/autosave/mac` on the server.

Four properties, each chosen deliberately:

* **Your repo is untouched.** Index, HEAD, branch and stash are all unchanged
  after a run -- verified before and after. It uses `git read-tree` + `write-tree`
  + `commit-tree` against a scratch `GIT_INDEX_FILE`, never `git add` on the real
  index. If `mktemp` ever fails the run aborts, because an empty `GIT_INDEX_FILE`
  silently means "use the real one".
* **Nothing reaches GitHub.** The push goes Mac -> your server. One of these
  repos is public; auto-pushing work in progress there would publish it.
* **The server is undisturbed.** `autosave/mac` is never the checked-out branch,
  so a phone session mid-run cannot be affected. Branch off it (`git checkout -b
  work autosave/mac`) rather than working on it: the Mac force-pushes that ref.
* **`.gitignore` is honoured**, so `node_modules` never crosses the wire. On
  these repos that is ~500 tracked files instead of 4 GB.

Install: copy `mac/ccr-autosave` to `~/bin/`, list your folders in
`~/.config/ccremote/autosave.conf` (TAB-separated: local path, then the folder
name on the server), copy `mac/com.ccremote.autosave.plist` into
`~/Library/LaunchAgents/` and `launchctl load` it. Log: `/tmp/ccr-autosave.log`.

**Keep the folders out of `~/Downloads`, `~/Desktop` and `~/Documents`.** macOS
TCC blocks login agents from reading those, and git reports the denial as a
bogus "no HEAD" -- which cost an hour of misdiagnosis here. Anywhere else in
your home directory is fine.

Limits: up to one interval of lag; nothing syncs while the Mac is asleep or
offline (the last snapshot stays on the server and remains usable); non-git
folders are skipped, so `git init` anything you want covered.

## Permissions: measured, not assumed

Tested against `claude 2.1.257`. The plan's guardrail — "use `--allowedTools`
allowlists instead of `--dangerously-skip-permissions`" — is **not sufficient on
its own**:

| configuration | what actually happened |
|---|---|
| `--permission-mode acceptEdits` + `--allowedTools Read` | asked to `rm -f` a file, **it ran and deleted the file** |
| `--permission-mode dontAsk` + `--allowedTools Read Write` | denied, `permission_denials: ['Bash']`, file survived |
| `--permission-mode dontAsk` + `--allowedTools "Bash(ls*)"` | `ls` ran; `rm` denied |
| `--tools "Read,Glob,Grep,Edit,Write"` | the Bash tool does not exist in the session at all |
| `--allowedTools "Bash(ls*)"`, asked for `ls -1 ; rm -f x` | denied — chained commands are checked per part |

So:

* **`permission_mode: dontAsk`** is what makes an allowlist binding. It denies
  instead of proceeding, and it never hangs on a prompt nobody can answer.
* **`allowed_tools:`** is additive. It says what may proceed without asking; by
  itself it restricts nothing.
* **`tools:`** is the hard boundary — the tool is not in the session. Use it for
  anything that must never write or run commands (see the `readonly` profile).
* Denials surface in the app as `N denied` on the run stamp, so a silently
  hobbled run is visible rather than mysterious.

Nothing here ever passes `--dangerously-skip-permissions`.

`--max-turns` is a hidden flag: absent from `--help`, but accepted and working
(`--max-turns 1` returns a normal result envelope). `--max-budget-usd` is the
documented ceiling and the one this profile system uses.

---

## Billing

Claude Code inherits a **clean environment** — `PATH`, `HOME`, `USER`, `SHELL`,
`LANG`, `TZ`, `TMPDIR`, `TERM`, `SSH_AUTH_SOCK`, and nothing else. Anything more
must be named in `CCR_PASS_ENV`.

That makes the switch the plan asked for explicit and un-flippable by accident:

* **Subscription** (default): no `ANTHROPIC_API_KEY` reaches the process.
* **API metering**: set `ANTHROPIC_API_KEY` *and* `CCR_PASS_ENV=ANTHROPIC_API_KEY`.

This matters more than it looks. Copying the parent environment wholesale — the
obvious implementation — hands over any stray `ANTHROPIC_API_KEY` and moves your
billing without a word.

The cost shown per run is Claude Code's own `total_cost_usd`, which is list-price
basis. On a subscription it is a relative measure of how heavy a run was, not an
invoice.

---

## The API

Everything except `/api/health` needs `X-Token: <CCR_TOKEN>`.

| endpoint | does |
|---|---|
| `GET /api/folders?path=` | sub-folders of `path`, plus its parent and breadcrumb |
| `POST /api/folders` | `{parent, name}` → creates a folder inside the workspace |
| `POST /api/sessions` | `{dir, prompt}` (or `{project, …}` for a pin) → returns `session_id` |
| `POST /api/sessions/{id}/messages` | `{prompt}` → next turn, resumes with full memory |
| `GET /api/sessions/{id}/messages?after=N` | transcript since seq N (the phone polls this) |
| `GET /api/sessions` | every session: status, cost, turns, folder |
| `POST /api/sessions/{id}/stop` | kills the running process |
| `POST /api/reload` | re-reads `tasks.yaml` without a restart |

Turns of one session are serialised by a per-session lock, so a follow-up sent
while Claude is still working queues instead of racing. `CCR_MAX_CONCURRENT`
caps how many sessions run at once, which is what keeps a burst of prompts from
eating the 5-hour usage window.

---

## Operating it

```bash
systemctl status ccremote
journalctl -u ccremote -f              # every run, by session id
sqlite3 /opt/ccremote/data/runs.sqlite3 'select project,status,cost_usd,title from sessions'
```

To take a session over in the terminal, the session id in the app *is* the
Claude Code session id:

```bash
ssh claude@<host>
cd <the project folder>
claude --resume <session-id>
```

That is the same conversation, continued interactively — the phone starts work,
`claude remote-control` or a plain `tmux` session finishes it.

## What was verified, and how

Measured on a real Ubuntu 24.04 VM, not reasoned about:

* `bootstrap.sh` runs clean end to end, exit 0.
* **The service self-starts at boot with nobody logged in** — boot 19:09:44,
  service active 19:09:49.
* SIGKILL the orchestrator mid-task and the session is marked `interrupted`,
  never stuck in `running`; the next message resumes the conversation with its
  memory intact. 11/11 crash-recovery tests pass.
* The process holds no controlling TTY, so a dropped SSH session cannot kill it.
* A phone on cellular, with wifi off, loaded the app and ran a real Claude Code
  session that wrote a file on the server.

The one thing no local test can prove is that the machine stays powered on.
That is what you are buying.

Bugs this testing found, all fixed here: `CLAUDE_CODE_OAUTH_TOKEN` missing from
the runner's environment allowlist (headless auth was impossible), `bubblewrap`
and `socat` missing from the package list, no swapfile, `Bash(cat*)` in the
default profile (it reads any file on the host), `.sheet{display:flex}` beating
the UA's `[hidden]{display:none}` (the settings sheet would not close and the
"Working…" bar never cleared), and static assets served with an ETag but no
`Cache-Control`, so every future fix would appear not to work.

## Notes

* Sessions survive an orchestrator restart. Anything mid-flight is marked
  `interrupted`; the conversation itself is intact and resumes on your next message.
* `branch_per_session: true` gives a project its own `git worktree` and `ccr/<id>`
  branch per session. Off by default: your prompt runs in the folder you named.
* ntfy push is optional and off unless `CCR_NTFY_URL` is set. Nothing depends on it.
