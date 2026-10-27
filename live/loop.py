#!/usr/bin/env python3
"""Fetch quotes every ~45s during a Taipei trading session and force-push an orphan data branch.

Pages builds from main, so these commits stay on `data` and do not rebuild the site.
The page reads live.json by commit SHA (raw.githubusercontent.com), which is uncached.
"""
import datetime, json, os, shutil, subprocess, sys, time, urllib.error, urllib.request

TZ = datetime.timezone(datetime.timedelta(hours=8))
INTERVAL = 45
MAX_MIN = 340
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def now():
    return datetime.datetime.now(TZ)

# Cash session 08:45–13:50 is 305 minutes. The runner cap is 360, so this job can
# wait for the open and still cover the whole session only when the wait is short.
DAY_INLINE_WAIT = 45 * 60
# A delayed night cron lands in the 05:05–08:45 gap. Bridge that whole gap; anything
# earlier than ~04:45 is not this handoff (Monday's 05:20 cron covers the pre-dawn).
DAY_BRIDGE = 4 * 3600
SHORT_WAIT = 15 * 60  # night open only — unchanged

def in_session(t):
    wd, hm = t.weekday(), t.hour * 60 + t.minute  # Mon=0
    if wd <= 4 and (8 * 60 + 45) <= hm <= (13 * 60 + 50):
        return True  # day Mon–Fri 08:45–13:50
    if wd <= 4 and hm >= 15 * 60:
        return True  # night Mon–Fri 15:00–24:00
    if 1 <= wd <= 5 and hm <= (5 * 60 + 5):
        return True  # tail Tue–Sat 00:00–05:05 (includes Sat early morning)
    return False

def session_action(t):
    """What a run should do at time t.

    "run"    — already inside a session
    "wait"   — sleep `seconds`, then trade in this job
    "bridge" — sleep `seconds` (until DAY_INLINE_WAIT before 08:45), dispatch a
               fresh run, and exit. The fresh run has a full runner budget for
               the cash session. Used when a start in the post-night gap is too
               early to wait and trade in one job.
    "exit"   — no session soon enough to stay up
    """
    if in_session(t):
        return ("run", 0.0)
    if t.weekday() > 4:
        return ("exit", 0.0)
    day = t.replace(hour=8, minute=45, second=0, microsecond=0)
    day_delta = (day - t).total_seconds()
    if 0 < day_delta <= DAY_BRIDGE:
        if day_delta <= DAY_INLINE_WAIT:
            return ("wait", day_delta)
        return ("bridge", day_delta - DAY_INLINE_WAIT)
    night = t.replace(hour=15, minute=0, second=0, microsecond=0)
    night_delta = (night - t).total_seconds()
    if 0 < night_delta <= SHORT_WAIT:
        return ("wait", night_delta)
    return ("exit", 0.0)

def sleep_for(seconds, label):
    end = time.monotonic() + max(0, seconds)
    next_log = 0.0
    while True:
        remain = end - time.monotonic()
        if remain <= 0:
            return
        if time.monotonic() >= next_log:
            print("%s %ds" % (label, int(remain)), flush=True)
            next_log = time.monotonic() + 300
        time.sleep(min(remain, 30))

def dispatch_live():
    """Start a fresh live-intraday run. GITHUB_TOKEN may dispatch workflows."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "laskin77/market-dashboard")
    if not token:
        print("no token, cannot dispatch", file=sys.stderr, flush=True)
        return False
    url = "https://api.github.com/repos/%s/actions/workflows/live.yml/dispatches" % repo
    req = urllib.request.Request(
        url,
        data=json.dumps({"ref": "main"}).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "market-dashboard-live",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("dispatched live-intraday", resp.status, flush=True)
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print("dispatch failed", e.code, detail, file=sys.stderr, flush=True)
        return False
    except Exception as e:
        print("dispatch failed", e, file=sys.stderr, flush=True)
        return False

def enter_session():
    """Block until this job should trade. False means the run should stop."""
    while True:
        action, secs = session_action(now())
        if action == "run":
            print("in session", now().isoformat(timespec="seconds"), flush=True)
            return True
        if action == "wait":
            print("waiting", int(secs), "s for session open", flush=True)
            sleep_for(secs, "waiting")
            continue
        if action == "bridge":
            print("early for day open; sleeping", int(secs), "s then handing off", flush=True)
            sleep_for(secs, "bridging")
            if dispatch_live():
                return False
            print("handoff failed; covering the open in this job", flush=True)
            continue
        print("outside session, exit", now().isoformat(timespec="seconds"), flush=True)
        return False

def publish(out_dir):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "laskin77/market-dashboard")
    if not token:
        print("no token, skip publish", file=sys.stderr)
        return False
    live = os.path.join(out_dir, "live.json")
    if not os.path.exists(live):
        print("no live.json", file=sys.stderr)
        return False
    work = "/tmp/live-publish"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    shutil.copy(live, os.path.join(work, "live.json"))
    intra = os.path.join(out_dir, "intraday")
    if os.path.isdir(intra) and os.listdir(intra):
        shutil.copytree(intra, os.path.join(work, "intraday"))
    close = os.path.join(out_dir, "close.json")
    if os.path.exists(close):
        shutil.copy(close, os.path.join(work, "close.json"))
    subprocess.check_call(["git", "init", "-q", "-b", "data"], cwd=work)
    subprocess.check_call(["git", "config", "user.email", "41898208+github-actions[bot]@users.noreply.github.com"], cwd=work)
    subprocess.check_call(["git", "config", "user.name", "github-actions[bot]"], cwd=work)
    subprocess.check_call(["git", "add", "-A"], cwd=work)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.check_call(["git", "commit", "-q", "-m", f"live {stamp}"], cwd=work)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    subprocess.check_call(["git", "push", "--force", "-q", url, "HEAD:data"], cwd=work)
    print("pushed", sha, stamp, flush=True)
    return True

def maybe_close(out_dir):
    """After the cash close, rebuild the static report once official files are up.

    GitHub cron often skips a slot. The night loop is already running by 15:00,
    so it retries every 10 minutes from 14:30 to 20:00. close.py itself no-ops
    once today's file is complete, and again until T86/BFI82U arrive after 16:30.
    """
    t = now()
    if t.weekday() > 4:
        return
    hm = t.hour * 60 + t.minute
    if hm < 14 * 60 + 30 or hm > 20 * 60:
        return
    stamp_path = "/tmp/mkt-close-stamp"
    try:
        if time.time() - float(open(stamp_path).read()) < 600:
            return
    except Exception:
        pass
    with open(stamp_path, "w") as f:
        f.write(str(time.time()))
    env = os.environ.copy()
    env["OUT_DIR"] = out_dir
    print("close rebuild", t.isoformat(timespec="seconds"), flush=True)
    subprocess.call([sys.executable, os.path.join(ROOT, "live", "close.py")], env=env)


def cycle(out_dir):
    maybe_close(out_dir)
    env = os.environ.copy()
    env["OUT_DIR"] = out_dir
    t = now()
    hm = t.strftime("%H%M")
    env["STOCKS"] = "1" if ("0900" <= hm <= "1345" and t.weekday() <= 4) else "0"
    subprocess.call([sys.executable, os.path.join(ROOT, "live", "fetch.py")], env=env)
    publish(out_dir)

def run_loop():
    out_dir = os.environ.get("OUT_DIR") or "/tmp/mkt-data"
    os.makedirs(out_dir, exist_ok=True)
    deadline = time.monotonic() + MAX_MIN * 60
    once = os.environ.get("LOOP_ONCE") == "1"
    while time.monotonic() < deadline:
        if not in_session(now()):
            print("session ended", now().isoformat(timespec="seconds"), flush=True)
            break
        t0 = time.monotonic()
        try:
            cycle(out_dir)
        except subprocess.CalledProcessError as e:
            print("cycle failed", e, file=sys.stderr, flush=True)
        if once:
            break
        remain = INTERVAL - (time.monotonic() - t0)
        if remain > 0 and time.monotonic() + remain < deadline:
            time.sleep(remain)
    print("loop exit", flush=True)

def main():
    if not enter_session():
        return 0
    run_loop()
    # The night loop exits at 05:05, hours before the cash open, and the morning
    # crons are often late or dropped. Hand the day session to a fresh runner
    # instead of exiting. Night-to-US handoff is unchanged: this only fires when
    # today's 08:45 open is still ahead.
    if session_action(now())[0] in ("wait", "bridge"):
        print("day open still ahead; dispatching a fresh run", flush=True)
        if not dispatch_live() and enter_session():
            run_loop()
    return 0

if __name__ == "__main__":
    sys.exit(main())
