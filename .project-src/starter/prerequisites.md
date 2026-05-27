# Prerequisites — Things only YOU can do

These are blockers that Claude Code cannot resolve on its own. Complete them before running the kickoff prompt.

Estimated time: **15–25 minutes.**

---

## 1. Identify your NFL.com league ID (2 minutes)

1. Open a browser, log into `fantasy.nfl.com`, and navigate to your league's home page.
2. Look at the URL. It will look like:
   ```
   https://fantasy.nfl.com/league/1234567
   ```
   The number after `/league/` is your **`LEAGUE_ID`**. Write it down.

---

## 2. Capture your NFL.com session cookie (5 minutes)

This is the most important step. NFL.com has no API key program for fantasy data — the only way for the crawler to access your private league is to impersonate your logged-in browser session.

### Steps (Chrome/Edge/Brave):

1. Log into `fantasy.nfl.com` and stay on your league page.
2. Open DevTools: right-click anywhere on the page → **Inspect** → click the **Network** tab.
3. Refresh the page (Cmd/Ctrl + R).
4. Click any request in the Network panel (the first row is fine).
5. In the right pane, scroll the **Headers** tab to find **Request Headers** → look for `Cookie:`.
6. Right-click the value next to `cookie:` → **Copy value**. This is one very long string — you want all of it.

### Steps (Firefox):

1. Same setup as above, but the path is: DevTools → Storage tab → **Cookies** → `https://fantasy.nfl.com`.
2. You'll see a list of cookie name/value pairs. You can either:
   - Copy each cookie's value individually (more work), or
   - Use the Network tab approach (same as Chrome) to grab the full `Cookie:` header in one shot.

### What you should have

A string that looks roughly like:
```
s_ecid=MCMID%7C12345...; nflnext-token=eyJ0eX...; AMCV_...=MCMID|...; userId=12345; ...
```

It will probably be **2,000+ characters long**. That's expected.

> ⚠️ **Treat this cookie like a password.** Anyone with it can read and modify your fantasy team. You'll paste it into a `.env` file that's git-ignored. Never commit it.

---

## 3. Know your cookie's lifetime (1 minute, just be aware)

NFL.com session cookies typically last **about 30 days** when "remember me" is enabled, but can be invalidated sooner if:
- You log out manually
- You log in from a different device that NFL flags as suspicious
- NFL rotates its auth system (rare, but happens)

**Mitigation built into Phase 1:** the crawler will detect auth failure (HTTP 302 to login, or empty/missing data) and log a clear error telling you to refresh the cookie. There will be a single command to update it.

---

## 4. Decide which scoring rules matter to you (5 minutes)

Open your league settings on NFL.com and screenshot or note down:

- **Passing**: points per yard, per TD, per INT, bonus thresholds (e.g., 300+ yard bonus)
- **Rushing**: same shape
- **Receiving**: same shape, plus PPR (full / half / none)
- **Kicking**: FG values by distance bracket
- **Defense/ST**: sacks, INTs, fumbles, TDs, points-allowed brackets, yards-allowed brackets
- **Bench / IR / Flex slot count**
- **Keepers**: how many, what cost (round penalty, salary, etc.)

Save these to a file or just keep the screenshot — the scoring-engine config will need them. Phase 1 will also **scrape** these from NFL.com, but having your own copy lets you spot-check the scraped result.

---

## 5. Install Claude Code (if you haven't) (3 minutes)

If you're already up to speed here, skip. Otherwise:
```bash
# macOS / Linux
curl -fsSL https://claude.ai/install.sh | sh

# or, via npm
npm install -g @anthropic-ai/claude-code
```

Verify: `claude --version`

---

## 6. Set up the project directory (2 minutes)

```bash
mkdir -p ~/code/fantasy-football
cd ~/code/fantasy-football
git init
```

Copy the files from `phase1_handoff/starter/` into this directory:
- `.env.example` → rename to `.env` and fill in the values
- `pyproject.toml`
- `.gitignore`
- `README.md`

Then copy the entire `phase1_handoff/docs/` folder into `./docs/`.

Verify with:
```bash
ls -la
cat .env  # should show your cookie + league ID populated
```

---

## 7. Confirm Python 3.11+ is available (1 minute)

```bash
python3 --version
# Expected: Python 3.11.x or 3.12.x or 3.13.x
```

If not, install via `pyenv`, `uv`, or your OS package manager. The starter `pyproject.toml` pins to `>=3.11`.

---

## You're ready when…

- [ ] You can paste your **`LEAGUE_ID`** number from memory
- [ ] Your `.env` file contains `NFL_COOKIE=...` with a real cookie string
- [ ] You have your **scoring rules** screenshot or notes handy
- [ ] `claude --version` works in your terminal
- [ ] `python3 --version` shows 3.11 or newer
- [ ] You're in the project directory with `docs/` populated and `.env` (gitignored)

Now open `CLAUDE_CODE_KICKOFF.md` and follow it.
