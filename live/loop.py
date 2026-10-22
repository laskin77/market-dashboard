#!/usr/bin/env python3
"""Fetch quotes every ~45s during a Taipei trading session and force-push an orphan data branch.

Pages builds from main, so these commits stay on `data` and do not rebuild the site.
The page reads live.json by commit SHA (raw.githubusercontent.com), which is uncached.
"""
import datetime, os, shutil, subprocess, sys, time

TZ = datetime.timezone(datetime.timedelta(hours=8))
INTERVAL = 45
MAX_MIN = 340
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def now():
    return datetime.datetime.now(TZ)

def in_session(t):
    wd, hm = t.weekday(), t.hour * 60 + t.minute  # Mon=0
    if wd <= 4 and (8 * 60 + 45) <= hm <= (13 * 60 + 50):
        return True  # day Mon–Fri 08:45–13:50
    if wd <= 4 and hm >= 15 * 60:
        return True  # night Mon–Fri 15:00–24:00
    if 1 <= wd <= 5 and hm <= (5 * 60 + 5):
        return True  # tail Tue–Sat 00:00–05:05 (includes Sat early morning)
    return False

def wait_until_open():
    t = now()
    if in_session(t):
        print("in session", t.isoformat(timespec="seconds"), flush=True)
        return True
    waits = []
    if t.weekday() <= 4:
        for hm in (8 * 60 + 45, 15 * 60):
            start = t.replace(hour=hm // 60, minute=hm % 60, second=0, microsecond=0)
            delta = (start - t).total_seconds()
            if 0 < delta <= 15 * 60:
                waits.append(delta)
    if not waits:
        print("outside session, exit", t.isoformat(timespec="seconds"), flush=True)
        return False
    print("waiting", int(min(waits)), "s for session open", flush=True)
    time.sleep(min(waits))
    return True

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

def main():
    if not wait_until_open():
        return 0
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
    return 0

if __name__ == "__main__":
    sys.exit(main())
