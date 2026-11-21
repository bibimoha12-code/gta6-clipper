#!/usr/bin/env python3
"""GTA 6 Clipper: stream link (or nothing) -> ready-to-post vertical clips.

  discover            find today's biggest GTA 6 streams (YouTube + Twitch)
  prep URL [URL...]   steps 1+2: audio-only download, local transcript, loud moments,
                      chat speed-ups (YouTube replays), 12-minute chunks for the AI to read
  status RUN          show what a run has and what's left
  render RUN          step 4: rank the AI's picks, download only those minutes,
                      find the facecam, stack facecam over gameplay, hook + word captions,
                      post caption + hashtags

Step 3 (picking) is done by the Claude Code session itself (no API keys); see SKILL.md.
Run with the skill's venv:  .venv/bin/python scripts/gta6clip.py <command>
"""
import argparse, concurrent.futures as cf, datetime as dt, json, math, os, re, shutil
import statistics, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
RUNS = Path(os.environ.get("GTA6_CLIPS_DIR", Path.home() / "GTA6-Clips"))
CFG = json.loads((SKILL / "config.json").read_text()) if (SKILL / "config.json").exists() else {}
CHUNK_SEC = 12 * 60
YTDLP = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--quiet", "--no-progress"]
# tools installed inside the venv (deno on Linux) must be findable by yt-dlp
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
# ffmpeg ignores HTTPS_PROXY and only reads http_proxy (used for https URLs too)
if os.environ.get("HTTPS_PROXY") and not os.environ.get("http_proxy"):
    os.environ["http_proxy"] = os.environ["HTTPS_PROXY"]
GTA_RE = re.compile(r"\bgta\s*(6|vi)\b|grand theft auto\s*(6|vi)\b", re.I)


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cookie_args(c):
    if not c:
        return []
    return ["--cookies", c] if os.path.isfile(c) else ["--cookies-from-browser", c]


def hms(s):
    s = max(0, int(round(s)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def slug(s, n=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s or "stream").strip("-").lower()
    return s[:n] or "stream"


def ytdlp_json(url, cookies=None, extra=()):
    r = run(YTDLP + cookie_args(cookies) + list(extra) + ["-J", url])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "yt-dlp failed")
    return json.loads(r.stdout)


# ---------------------------------------------------------------- discover
def load_env():
    env = {}
    f = SKILL / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return {**env, **os.environ}


def helix(path, cid, token):
    req = urllib.request.Request(f"https://api.twitch.tv/helix/{path}",
                                 headers={"Client-ID": cid, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def discover_twitch(limit):
    """Official Twitch API. Needs a free Twitch dev app (dev.twitch.tv/console):
    TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET in the skill's .env. Skipped without them."""
    env = load_env()
    cid, sec = env.get("TWITCH_CLIENT_ID"), env.get("TWITCH_CLIENT_SECRET")
    if not (cid and sec):
        log("  twitch: skipped (add TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET to .env to include Twitch)")
        return []
    out = []
    try:
        body = f"client_id={cid}&client_secret={sec}&grant_type=client_credentials".encode()
        with urllib.request.urlopen(urllib.request.Request("https://id.twitch.tv/oauth2/token", data=body), timeout=20) as r:
            tok = json.loads(r.read())["access_token"]
        games = helix("games?name=Grand%20Theft%20Auto%20VI", cid, tok).get("data") or []
        streams = helix(f"streams?game_id={games[0]['id']}&first=20", cid, tok).get("data", []) if games else []
        for st in streams[: limit * 2]:
            vids = helix(f"videos?user_id={st['user_id']}&type=archive&first=1", cid, tok).get("data") or []
            if vids:
                out.append({"platform": "twitch", "streamer": st["user_name"], "title": st["title"],
                            "viewers": st["viewer_count"], "live_now": True, "url": vids[0]["url"]})
    except Exception as e:
        log(f"  twitch: skipped ({e})")
    return out


def discover_youtube(limit):
    """Finished GTA 6 streams from today / this week (clippable now) + what's live right now."""
    out, seen = [], set()
    searches = [("GTA+6+stream", "EgIIAg%3D%3D"), ("GTA+6+live", "EgIIAg%3D%3D"),
                ("GTA+6+stream", "EgIIAw%3D%3D"), ("GTA+6", "EgJAAQ%3D%3D")]
    for q, sp in searches:
        try:
            pl = ytdlp_json(f"https://www.youtube.com/results?search_query={q}&sp={sp}",
                            extra=["--flat-playlist", "--playlist-end", "40"])
        except Exception as e:
            log(f"  youtube search skipped: {e}")
            continue
        for e in pl.get("entries") or []:
            if not e or e.get("id") in seen or not GTA_RE.search(e.get("title") or ""):
                continue
            live = e.get("live_status") == "is_live"
            if not live and (e.get("duration") or 0) < 40 * 60:  # regular videos aren't streams
                continue
            seen.add(e["id"])
            out.append({"platform": "youtube", "streamer": e.get("channel") or e.get("uploader") or "",
                        "title": e.get("title", ""), "live_now": live,
                        "viewers": (e.get("concurrent_view_count") or 0) * 20 if live else (e.get("view_count") or 0),
                        "length": hms(e.get("duration") or 0), "url": f"https://www.youtube.com/watch?v={e['id']}"})
    return out


def cmd_discover(a):
    log("Finding today's biggest GTA 6 streams...")
    with cf.ThreadPoolExecutor(2) as ex:
        tw, yt = ex.submit(discover_twitch, a.limit), ex.submit(discover_youtube, a.limit)
        streams = tw.result() + yt.result()
    # clippable first (finished YouTube replays + Twitch archives), YouTube still-live last
    streams.sort(key=lambda s: (s["platform"] == "youtube" and s["live_now"], -s["viewers"]))
    seen, picked = set(), []
    for s in streams:
        k = s["streamer"].lower()
        if k not in seen:
            seen.add(k)
            picked.append(s)
    picked = picked[: a.limit]
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"discover-{dt.date.today()}.json").write_text(json.dumps(picked, indent=2))
    for i, s in enumerate(picked, 1):
        tag = "LIVE NOW, clip after it ends" if s["platform"] == "youtube" and s["live_now"] else s.get("length", "archive")
        log(f"{i}. [{s['platform']}] {s['streamer']} ({tag}) {s['title'][:80]}\n   {s['url']}")
    if not picked:
        log("Nothing found. Paste stream links instead.")


# ---------------------------------------------------------------- prep (steps 1 + 2)
def loud_moments(wav):
    """Per-second RMS, flag seconds well above the local baseline (yelling / losing it)."""
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav), "-af",
             "asetnsamples=n=16000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"])
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", r.stdout):
        v = m.group(1)
        vals.append(-90.0 if v == "-inf" else float(v))
    events = []
    if not vals:
        return events, vals
    W = 90
    for i, v in enumerate(vals):
        if v < -32:
            continue
        lo, hi = max(0, i - W), min(len(vals), i + W)
        base = statistics.median(vals[lo:hi])
        if v - base >= 9:
            events.append((i, round(v - base, 1), base))
    # merge consecutive seconds into events
    merged = []
    for t, d, bs in events:
        if merged and t - merged[-1]["end"] <= 2:
            merged[-1]["end"] = t
            merged[-1]["db"] = max(merged[-1]["db"], d)
        else:
            merged.append({"start": t, "end": t, "db": d, "base": round(bs, 1)})
    return merged, vals


LAUGH_RE = re.compile(r"(?:[jx][aeiou]){2,}|(?:h[ae]){2,}|j[sk]j[sk]|\bxd+\b|\blo+l\b|\blmao|\blmfao|kekw|omegalul|"
                      r"\blul\b|icant|😂|🤣|💀|😭", re.I)


def chat_weight(text):
    """A laughing message counts double: a chat full of 'jajaja' marks a funny moment."""
    return 2 if LAUGH_RE.search(text or "") else 1


def spikes_from(events, end=None):
    """events: (stream_seconds, weight). 10-second bins of weighted messages vs the local baseline.
    A spike is 8+ weighted messages at 2.5x normal; `laugh` marks one that is mostly laughter."""
    events = [(t, w) for t, w in events if t >= 0]
    if not events:
        return []
    nb = int((end or max(t for t, _ in events)) // 10) + 1
    bins, laughs = [0] * nb, [0] * nb
    for t, w in events:
        i = int(t // 10)
        if i < nb:
            bins[i] += w
            laughs[i] += w if w > 1 else 0
    out = []
    for i, c in enumerate(bins):
        lo, hi = max(0, i - 30), min(nb, i + 30)
        base = max(1.0, statistics.median(bins[lo:hi]))
        if c >= 8 and c / base >= 2.5:
            out.append({"start": i * 10, "end": i * 10 + 10, "x": round(c / base, 1), "msgs": c,
                        "laugh": laughs[i] >= c * 0.5})
    return out


def chat_spikes(chat_file):
    """YouTube replay chat: weighted messages per 10 s vs the local baseline."""
    if not chat_file or not Path(chat_file).exists():
        return []
    events = []
    with open(chat_file) as f:
        for line in f:
            try:
                a = json.loads(line)["replayChatItemAction"]
                ms = int(a["videoOffsetTimeMsec"])
            except Exception:
                continue
            text = ""
            try:
                item = a["actions"][0]["addChatItemAction"]["item"]
                r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer") or {}
                for run_ in r.get("message", {}).get("runs", []):
                    text += run_.get("text") or "".join(run_.get("emoji", {}).get("shortcuts", [])[:1])
            except Exception:
                pass
            events.append((ms / 1000, chat_weight(text)))
    if len(events) < 50:
        return []
    return spikes_from(events)


PIECE_SEC = 10 * 60


def _whisper(wav, out_base, words=False):
    model = CFG.get("whisper_model") or os.path.expanduser("~/.cache/whisper-cpp/ggml-small.bin")
    cmd = [CFG.get("whisper_cli") or "whisper-cli", "-m", model, "-f", str(wav), "-l", CFG.get("whisper_lang") or "en",
           "-t", str(max(1, (os.cpu_count() or 4))), "-np", "-mc", "0", "-oj", "-of", str(out_base)]
    if words:
        cmd += ["-ml", "1", "-sow", "-ojf"]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError("whisper failed: " + r.stderr[-400:])
    data = json.loads(Path(f"{out_base}.json").read_text())
    segs = []
    for s in data.get("transcription", []):
        txt = s.get("text", "").strip()
        if txt:
            segs.append({"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000, "text": txt})
    return segs


def transcribe_range(wav, a, b, pdir, name):
    """Transcribe only [a, b) seconds of a long wav (cached per window)."""
    pdir = Path(pdir)
    pdir.mkdir(exist_ok=True)
    done = pdir / f"{name}.done.json"
    if not done.exists():
        pw = pdir / f"{name}.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(a), "-t", str(b - a), "-i", str(wav), "-c", "copy", str(pw)])
        part = _whisper(pw, pdir / name)
        pw.unlink(missing_ok=True)
        done.write_text(json.dumps(part))
    return [{"start": sg["start"] + a, "end": sg["end"] + a, "text": sg["text"]} for sg in json.loads(done.read_text())]


TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # the public client id twitch.tv's own player uses
TWITCH_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def twitch_vod_chat_peaks(video_id, dur, step=20, cache=None):
    """Chat replay of a Twitch VOD, sampled every `step` s (the replay the twitch.tv player shows).
    Each sample's density = weighted messages per second (laughs count double). Returns chat
    explosions as candidates [{start, end, x, laugh, db}] strongest first; [] if chat can't be read."""
    vid = str(video_id).lstrip("v")
    samples = json.loads(Path(cache).read_text()) if cache and Path(cache).exists() else {}
    for off in range(0, int(dur), step):
        if str(off) in samples:
            continue
        body = json.dumps([{"operationName": "VideoCommentsByOffsetOrCursor",
                            "variables": {"videoID": vid, "contentOffsetSeconds": off},
                            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_COMMENTS_HASH}}}])
        r = run(["curl", "-fsS", "--retry", "2", "-m", "20", TWITCH_GQL, "-H", f"Client-Id: {TWITCH_WEB_CLIENT_ID}",
                 "-H", "Content-Type: application/json", "-d", body])
        try:
            edges = json.loads(r.stdout)[0]["data"]["video"]["comments"]["edges"]
        except Exception:
            if not samples:
                log("  twitch chat replay not readable, falling back to loudness")
                return []
            continue
        ev = []
        for e in edges:
            n = e["node"]
            txt = "".join(f.get("text") or "" for f in n["message"]["fragments"])
            ev.append((n["contentOffsetSeconds"], chat_weight(txt)))
        ev = [x for x in ev if off <= x[0] < off + step]
        span = max(5, (max(t for t, _ in ev) - off + 1)) if ev else step
        samples[str(off)] = [round(sum(w for _, w in ev) / span, 3),
                             round(sum(w for _, w in ev if w > 1) / max(1, sum(w for _, w in ev)), 2)]
        if cache and len(samples) % 50 == 0:
            Path(cache).write_text(json.dumps(samples))
            log(f"    chat: {len(samples)}/{int(dur) // step} samples")
        time.sleep(0.15)
    if cache:
        Path(cache).write_text(json.dumps(samples))
    offs = sorted(int(k) for k in samples)
    dens = [samples[str(o)][0] for o in offs]
    out = []
    for i, o in enumerate(offs):
        base = max(0.05, statistics.median(dens[max(0, i - 15):i + 15]))
        x = dens[i] / base
        if x >= 2.0:
            laugh = samples[str(o)][1] >= 0.5
            # chat reacts after the moment: centre the window ~15 s before the burst
            out.append({"start": max(0, o - 15), "end": o + 5, "x": round(x, 1), "laugh": laugh,
                        "db": round(x * (1.5 if laugh else 1), 1)})
    return sorted(out, key=lambda e: e["db"], reverse=True)


def quick_candidates(loud):
    """Loud moments worth a look, strongest first. A real yell rises 9-30 dB over the streamer's normal
    talking level; a jump of 30+ dB, or one from a near-silent baseline, is music or a cutscene starting
    after a quiet stretch, not a reaction."""
    real = [e for e in loud if e["db"] <= 30 and e.get("base", -30) >= -45]
    return sorted(real, key=lambda e: e["db"] + min(e["end"] - e["start"], 6), reverse=True)


def spoken_words(segs):
    """Words actually said (whisper tags like [MÚSICA], (música), *risa* don't count)."""
    txt = " ".join(re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪[^♪]*♪?", " ", s["text"]) for s in segs)
    return len(re.findall(r"\w{2,}", txt))


def transcribe(wav, out_base, words=False):
    """Long audio is transcribed in 10-minute pieces, each saved as it finishes, so an interrupted
    prep picks up where it stopped instead of starting the whole stream again."""
    dur = (Path(wav).stat().st_size - 44) / 32000  # 16 kHz mono s16 wav
    if words or dur <= 1.5 * PIECE_SEC:
        return _whisper(wav, out_base, words)
    pdir = Path(f"{out_base}_pieces")
    pdir.mkdir(exist_ok=True)
    segs, n = [], math.ceil(dur / PIECE_SEC)
    for i in range(n):
        done = pdir / f"piece_{i:03d}.done.json"
        if not done.exists():
            log(f"    transcribing piece {i + 1}/{n}")
            pw = pdir / f"piece_{i:03d}.wav"
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(i * PIECE_SEC), "-t", str(PIECE_SEC),
                 "-i", str(wav), "-c", "copy", str(pw)])
            part = _whisper(pw, pdir / f"piece_{i:03d}")
            pw.unlink(missing_ok=True)
            done.write_text(json.dumps(part))
        for sg in json.loads(done.read_text()):
            segs.append({"start": sg["start"] + i * PIECE_SEC, "end": sg["end"] + i * PIECE_SEC, "text": sg["text"]})
    return segs


def download_audio(url, cookies, rd, meta):
    """Audio-only download. Twitch VODs are HLS: hand the playlist straight to ffmpeg, reading from the
    first segment (a VOD that is still recording is an EVENT playlist, which yt-dlp would fetch at
    real-time speed). Everything else goes through yt-dlp."""
    if any(k in (meta.get("extractor_key") or "").lower() for k in ("twitch", "kick")):
        g = run(YTDLP + cookie_args(cookies) + ["-f", "Audio_Only/audio_only/worst", "-g", url])
        hls = g.stdout.strip().splitlines()[-1] if g.returncode == 0 and g.stdout.strip() else ""
        if hls:
            dur = ["-t", str(int(meta["duration"]))] if meta.get("duration") else []
            r = run(["ffmpeg", "-y", "-loglevel", "error", "-live_start_index", "0", "-i", hls, *dur,
                     "-vn", "-c:a", "copy", str(rd / "audio.m4a")])
            if r.returncode == 0:
                return r
    return run(YTDLP + cookie_args(cookies) + ["--no-playlist", "-N", "8", "-f", "bestaudio/Audio_Only/audio_only/worst",
                                               "-o", str(rd / "audio.%(ext)s"), url])


def prep_one(url, cookies, force=False, quick=0, chat=False):
    t0 = time.time()
    meta = ytdlp_json(url, cookies, ["--no-playlist"])
    if meta.get("live_status") == "is_live" and meta.get("extractor_key", "").lower().startswith("youtube"):
        return {"url": url, "error": "still live on YouTube: clip it once the replay is up (Twitch archives work while live)"}
    streamer = meta.get("uploader") or meta.get("channel") or "streamer"
    rid = f"{dt.date.today()}-{slug(streamer, 24)}-{slug(str(meta.get('id')), 16)}"
    rd = RUNS / rid
    (rd / "chunks").mkdir(parents=True, exist_ok=True)
    (rd / "picks").mkdir(exist_ok=True)
    info = {"url": url, "webpage_url": meta.get("webpage_url") or url, "id": meta.get("id"),
            "title": meta.get("title"), "streamer": streamer, "platform": meta.get("extractor_key"),
            "duration": meta.get("duration"), "cookies": cookies}
    (rd / "stream.json").write_text(json.dumps(info, indent=2))
    if (rd / "transcript.json").exists() and not force:
        log(f"[{streamer}] already prepped -> {rd}")
        return {"run": str(rd), "streamer": streamer, "cached": True}

    wav = rd / "audio.wav"
    is_yt = "youtube" in (meta.get("extractor_key") or "").lower()
    if wav.exists():
        log(f"[{streamer}] 1/2 audio already downloaded, resuming")
    else:
        log(f"[{streamer}] 1/2 downloading audio only ({hms(meta.get('duration') or 0)} stream)")
        with cf.ThreadPoolExecutor(2) as ex:
            fa = ex.submit(download_audio, url, cookies, rd, meta)
            fc = ex.submit(run, YTDLP + cookie_args(cookies) + ["--no-playlist", "--skip-download", "--write-subs",
                                                                 "--sub-langs", "live_chat", "-o", str(rd / "chat"), url]) if is_yt else None
            ra = fa.result()
            if fc:
                fc.result()
        src = next((p for p in rd.glob("audio.*") if p.suffix not in (".part", ".ytdl", ".wav")), None)
        if not src:
            raise RuntimeError(f"audio download failed: {ra.stderr[-300:]}")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", "-f", "wav", str(rd / "audio.wav.part")])
        (rd / "audio.wav.part").rename(wav)  # only a complete wav counts as downloaded
        src.unlink(missing_ok=True)
    t_dl = time.time()

    if quick:
        log(f"[{streamer}] 2/2 quick mode: finding the loudest moments, transcribing only around the top {quick}")
        loud, _ = loud_moments(wav)
        dur_ = meta.get("duration") or 1e9
        ranges, segs, tried = [], [], 0
        cands = quick_candidates(loud)
        if chat and "twitch" in (meta.get("extractor_key") or "").lower():
            log(f"[{streamer}]   reading the chat replay (every 20 s)")
            peaks = twitch_vod_chat_peaks(meta.get("id"), meta.get("duration") or 0, cache=rd / "chat_samples.json")
            (rd / "chat_peaks.json").write_text(json.dumps(peaks[:100], indent=1))
            if peaks:
                cands = peaks
        for e in cands:
            if len(ranges) >= quick or tried >= quick * 3:
                break
            if any(abs(e["start"] - (a_ + 90)) < 180 for a_, _ in ranges):
                continue
            tried += 1
            a_, b_ = max(0, e["start"] - 90), min(dur_, e["end"] + 60)
            part = transcribe_range(wav, a_, b_, rd / "whisper_pieces", f"win_{int(a_)}")
            near = [sg for sg in part if sg["end"] >= e["start"] - 30 and sg["start"] <= e["end"] + 30]
            if spoken_words(near) < 15:  # nobody is talking around the peak: music / cutscene
                log(f"    skip {hms(e['start'])} (+{e['db']:.0f} dB): no speech around it")
                continue
            log(f"    window {len(ranges) + 1}/{quick}  {hms(a_)}-{hms(b_)}  (+{e['db']:.0f} dB)")
            ranges.append((a_, b_))
            segs += part
        order = sorted(range(len(ranges)), key=lambda i: ranges[i][0])
        ranges = [ranges[i] for i in order]
        segs.sort(key=lambda sg: sg["start"])
    else:
        log(f"[{streamer}] 2/2 transcribing on this Mac + scanning for loud moments and chat spikes")
        with cf.ThreadPoolExecutor(2) as ex:
            fs = ex.submit(transcribe, wav, rd / "whisper")
            fl = ex.submit(loud_moments, wav)
            segs = fs.result()
            loud, _ = fl.result()
        ranges = None
    chat_file = next(iter(rd.glob("chat*.live_chat.json")), None)
    chat = chat_spikes(chat_file)
    wav.unlink(missing_ok=True)
    (rd / "whisper.json").unlink(missing_ok=True)
    (rd / "transcript.json").write_text(json.dumps(segs))
    (rd / "signals.json").write_text(json.dumps({"loud": loud, "chat": chat}, indent=1))

    # ---- 12-minute chunks with signal markers inline
    dur = meta.get("duration") or (segs[-1]["end"] if segs else 0)
    if ranges is None:
        ranges = [(ci * CHUNK_SEC, (ci + 1) * CHUNK_SEC) for ci in range(max(1, math.ceil(dur / CHUNK_SEC)))]
    n = len(ranges)
    for ci, (a, b) in enumerate(ranges):
        lines = [f"# {streamer}: {info['title']}", f"Chunk {ci + 1}/{n}  ({hms(a)} to {hms(b)})"
                 + ("  [quick mode: only the minutes around a loud moment]" if quick else ""),
                 "Markers: LOUD(+dB over normal) = streamer got loud. CHAT xN = chat moving N times faster than normal (laughs count double); LAUGH = mostly laughing.", ""]
        for s in segs:
            if s["end"] <= a or s["start"] >= b:
                continue
            tags = []
            for e in loud:
                if e["start"] <= s["end"] and e["end"] + 1 >= s["start"]:
                    tags.append(f"LOUD(+{e['db']:.0f}dB)")
                    break
            for e in chat:  # chat reacts a few seconds late
                if e["start"] - 15 <= s["end"] and e["end"] >= s["start"]:
                    tags.append(f"CHAT x{e['x']}" + (" LAUGH" if e.get("laugh") else ""))
                    break
            lines.append(f"[{hms(s['start'])}] {s['text']}" + (f"   <<{' '.join(tags)}>>" if tags else ""))
        (rd / "chunks" / f"chunk_{ci + 1:02d}.md").write_text("\n".join(lines) + "\n")
    timing = {"prep_started": t0, "download_sec": round(t_dl - t0), "analyse_sec": round(time.time() - t_dl),
              "chunks": n, "segments": len(segs), "loud_events": len(loud), "chat_spikes": len(chat)}
    (rd / "timing.json").write_text(json.dumps(timing, indent=1))
    log(f"[{streamer}] ready: {n} chunks, {len(loud)} loud moments, {len(chat)} chat spikes "
        f"({round(time.time() - t0)}s) -> {rd}")
    return {"run": str(rd), "streamer": streamer, "chunks": n}


def cmd_prep(a):
    RUNS.mkdir(parents=True, exist_ok=True)
    results = []
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(prep_one, u, a.cookies, a.force, a.quick, a.chat): u for u in a.urls}
        for f in cf.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"url": futs[f], "error": str(e)})
                log(f"FAILED {futs[f]}: {e}")
    log("\nRUNS:")
    for r in results:
        log(f"  {r.get('run') or ''}  {r.get('error') or ''}")


# ---------------------------------------------------------------- status
def cmd_status(a):
    rd = Path(a.run)
    chunks = sorted((rd / "chunks").glob("chunk_*.md"))
    picked = {p.stem for p in (rd / "picks").glob("chunk_*.json")}
    todo = [c.name for c in chunks if c.stem not in picked]
    cands = sum(len(json.loads(p.read_text())) for p in (rd / "picks").glob("chunk_*.json"))
    log(json.dumps({"run": str(rd), "chunks": len(chunks), "picked_chunks": len(picked), "candidates": cands,
                    "chunks_left": todo, "clips": sorted(x.name for x in (rd / "clips").glob("*.mp4"))
                    if (rd / "clips").exists() else []}, indent=1))


# ---------------------------------------------------------------- render (step 4)
def select(rd, top, min_score, max_sec=75):
    segs = json.loads((rd / "transcript.json").read_text())
    cands = []
    for p in sorted((rd / "picks").glob("chunk_*.json")):
        try:
            for c in json.loads(p.read_text()):
                c["_s"], c["_e"] = parse_ts(c["start"]), parse_ts(c["end"])
                cands.append(c)
        except Exception as e:
            log(f"  bad pick file {p.name}: {e}")
    total = len(cands)
    ok = []
    for c in sorted(cands, key=lambda c: -float(c.get("score", 0))):
        if float(c.get("score", 0)) < min_score:
            continue
        # snap to sentence boundaries so clips don't start/end mid-word
        st = max([s["start"] for s in segs if s["start"] <= c["_s"] + 0.5] or [c["_s"]])
        en = min([s["end"] for s in segs if s["end"] >= c["_e"] - 0.5] or [c["_e"]])
        if en - st > max_sec:
            en = st + max_sec
        if en - st < 12:
            continue
        if any(not (en + 20 < o["_s"] or st > o["_e"] + 20) for o in ok):
            continue
        c["_s"], c["_e"] = max(0, st - 0.25), en + 0.4
        ok.append(c)
        if len(ok) >= top:
            break
    return ok, total


def find_facecam(video, W, H):
    """Sample frames, find a face that sits in the same small spot (a facecam overlay),
    then find the overlay box edges from static straight lines around it."""
    import cv2
    import numpy as np
    cv2.setLogLevel(0) if hasattr(cv2, "setLogLevel") else None
    cap = cv2.VideoCapture(str(video))
    nfr = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 300
    det = None
    ym = CFG.get("yunet_model")
    if ym and Path(ym).exists():
        det = cv2.FaceDetectorYN.create(ym, "", (W, H), 0.75, 0.3, 50)
    haar = None
    if det is None and hasattr(cv2, "CascadeClassifier"):
        haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces, grads = [], []
    for i in range(12):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(nfr * (i + 0.5) / 12))
        ok, fr = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        grads.append((np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0)), np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1))))
        found = []
        if det is not None:
            _, res = det.detect(fr)
            if res is not None:
                found = [tuple(map(float, r[:4])) for r in res]
        elif haar is not None:
            found = [tuple(map(float, f)) for f in haar.detectMultiScale(g, 1.1, 6, minSize=(40, 40))]
        faces.append(found)
    cap.release()
    flat = [f for fs in faces for f in fs]
    if not flat:
        return {"mode": "gameplay"}
    # cluster by centre; keep the most persistent cluster
    best, best_n = None, 0
    for f in flat:
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        members = [g for g in flat if abs(g[0] + g[2] / 2 - cx) < W * 0.06 and abs(g[1] + g[3] / 2 - cy) < H * 0.08]
        if len(members) > best_n:
            best, best_n = members, len(members)
    if best_n < max(3, len(faces) * 0.4):
        return {"mode": "gameplay"}
    fx = statistics.median(m[0] for m in best); fy = statistics.median(m[1] for m in best)
    fw = statistics.median(m[2] for m in best); fh = statistics.median(m[3] for m in best)
    if fw > W * 0.2:  # face fills the screen: it's a camera view, not an overlay
        return {"mode": "cam", "face": [fx, fy, fw, fh]}

    gx = np.median(np.stack([g[0] for g in grads]), axis=0)  # vertical edges, static over time
    gy = np.median(np.stack([g[1] for g in grads]), axis=0)  # horizontal edges

    def scan(profile, start, stop):
        rng = range(int(start), int(stop), 1 if stop > start else -1)
        vals = [(profile[i], i) for i in rng if 0 <= i < len(profile)]
        if len(vals) < 5:
            return None
        v, i = max(vals)
        return i if v > 3.5 * (statistics.median(x for x, _ in vals) + 1e-3) and v > 6 else None

    y0, y1 = int(max(0, fy - fh * 0.5)), int(min(H, fy + fh * 1.5))
    x0, x1 = int(max(0, fx - fw * 0.5)), int(min(W, fx + fw * 1.5))
    colp = gx[y0:y1].mean(axis=0)
    rowp = gy[:, x0:x1].mean(axis=1)
    L = scan(colp, fx - fw * 0.15, fx - fw * 2.6)
    R = scan(colp, fx + fw * 1.15, fx + fw * 3.6)
    T = scan(rowp, fy - fh * 0.15, fy - fh * 2.2)
    B = scan(rowp, fy + fh * 1.15, fy + fh * 3.2)
    # fallbacks: typical facecam framing around the face
    cw_guess = fw * 3.4
    ch_guess = cw_guess * 0.75
    L = L if L is not None else fx + fw / 2 - cw_guess / 2
    R = R if R is not None else fx + fw / 2 + cw_guess / 2
    T = T if T is not None else fy + fh * 0.45 - ch_guess * 0.45
    B = B if B is not None else fy + fh * 0.45 + ch_guess * 0.55
    L, R = max(0, L + 2), min(W, R - 2)
    T, B = max(0, T + 2), min(H, B - 2)
    if R - L < fw * 1.4 or B - T < fh * 1.4:
        return {"mode": "gameplay"}
    return {"mode": "facecam", "box": [int(L), int(T), int(R - L), int(B - T)], "face": [fx, fy, fw, fh]}


def font(size):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
              "/usr/share/fonts/opentype/inter/Inter-Black.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def wrap(draw, text, fnt, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def caption_frames(words, hook, seam_y, cap_y, dur, outdir):
    """Build one transparent 1080x1920 PNG per caption state + an ffconcat list."""
    from PIL import Image, ImageDraw
    outdir.mkdir(exist_ok=True)
    hf, cf_ = font(50), font(84)

    def base():
        im = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if hook:
            lines = wrap(d, hook.upper(), hf, 900)[:3]
            lh = 62
            bh = lh * len(lines) + 34
            top = seam_y - bh // 2
            bw = max(d.textlength(l, font=hf) for l in lines) + 60
            d.rounded_rectangle([540 - bw / 2, top, 540 + bw / 2, top + bh], 18, fill=(255, 255, 255, 255))
            for i, l in enumerate(lines):
                d.text((540, top + 17 + i * lh + lh / 2), l, font=hf, fill=(0, 0, 0), anchor="mm")
        return im

    # group words into 1-3 word chunks
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.45 or re.search(r"[.?!,]$", cur[-1]["t"])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    states = []  # (t0, t1, group, active_idx)
    for g in groups:
        for i, w in enumerate(g):
            t1 = g[i + 1]["s"] if i + 1 < len(g) else w["e"]
            states.append((w["s"], max(t1, w["s"] + 0.08), g, i))
    timeline, t, k = [], 0.0, 0
    blank = outdir / "blank.png"
    base().save(blank)
    for t0, t1, g, ai in states:
        if t0 > t + 0.02:
            timeline.append((blank, t0 - t))
        im = base()
        d = ImageDraw.Draw(im)
        text = [w["t"].upper() for w in g]
        widths = [d.textlength(x, font=cf_) for x in text]
        space = d.textlength(" ", font=cf_)
        total = sum(widths) + space * (len(text) - 1)
        scale_font = cf_
        if total > 1000:
            scale_font = font(int(84 * 1000 / total))
            widths = [d.textlength(x, font=scale_font) for x in text]
            space = d.textlength(" ", font=scale_font)
            total = sum(widths) + space * (len(text) - 1)
        x = 540 - total / 2
        for j, (word, wd) in enumerate(zip(text, widths)):
            d.text((x, cap_y), word, font=scale_font, anchor="lm",
                   fill=(255, 221, 0) if j == ai else (255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0))
            x += wd + space
        p = outdir / f"c{k:04d}.png"
        im.save(p)
        k += 1
        start = max(t, t0)
        timeline.append((p, max(0.04, t1 - start)))
        t = start + max(0.04, t1 - start)
    if t < dur:
        timeline.append((blank, dur - t))
    lst = outdir / "list.ffconcat"
    body = ["ffconcat version 1.0"]
    for p, d_ in timeline:
        body += [f"file '{p.name}'", f"duration {d_:.3f}"]
    body.append(f"file '{timeline[-1][0].name}'")
    lst.write_text("\n".join(body) + "\n")
    return lst


def clip_words(src, ss, dur, work):
    wav = work / "w.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
         "-ac", "1", "-ar", "16000", str(wav)])
    segs = transcribe(wav, work / "words", words=True)
    words = []
    for s in segs:
        t = s["text"].strip()
        if not t or re.fullmatch(r"\[.*\]|\(.*\)", t):
            continue
        if re.fullmatch(r"[^\w]+", t) and words:
            words[-1]["t"] += t
            continue
        words.append({"t": t, "s": s["start"], "e": s["end"]})
    wav.unlink(missing_ok=True)
    return words


def twitch_section(url, cookies, a, b, work):
    """Twitch VOD: read the HLS playlist and fetch only the 10-second pieces covering [a, b], then join them.
    Much faster and sturdier than seeking with ffmpeg on an 8-hour playlist. Returns (file, real_start)."""
    g = run(YTDLP + cookie_args(cookies) + ["-f", "bv*[height<=1080][fps<=60]+ba/b[height<=1080]/b", "-g", url])
    m3u8 = (g.stdout.strip().splitlines() or [""])[0]
    if g.returncode != 0 or ".m3u8" not in m3u8:
        return None
    base = m3u8.rsplit("/", 1)[0] + "/"
    pl = run(["curl", "-fsSL", "--retry", "3", m3u8])
    if pl.returncode != 0:
        return None
    t, dur, init, picked = 0.0, None, None, []
    for ln in pl.stdout.splitlines():
        if ln.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', ln)
            init = m.group(1) if m else None
        elif ln.startswith("#EXTINF:"):
            dur = float(ln[8:].split(",")[0])
        elif ln and not ln.startswith("#") and dur is not None:
            if t + dur > a and t < b:
                picked.append((t, init, ln))
            t += dur
            dur = None
    if not picked:
        return None
    raw = work / "src_raw.mp4" if picked[0][1] else work / "src_raw.ts"
    with open(raw, "wb") as out:
        last_init = None
        for _, ini, seg in picked:
            for part in ([ini] if ini and ini != last_init else []) + [seg]:
                r = subprocess.run(["curl", "-fsSL", "--retry", "3", urllib.parse.urljoin(base, part)],
                                   capture_output=True)
                if r.returncode != 0:
                    return None
                out.write(r.stdout)
            last_init = ini
    src = work / "src.mp4"
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-fflags", "+genpts", "-i", str(raw), "-c", "copy", str(src)])
    raw.unlink(missing_ok=True)
    return (src, picked[0][0]) if r.returncode == 0 and src.exists() else None


def render_one(rd, info, c, n, cookies, keep, local_src=None, src_start=None):
    """local_src/src_start: a file already on disk that starts at stream time src_start
    (the live clipper cuts from its buffer instead of downloading)."""
    clips = rd / "clips"
    clips.mkdir(exist_ok=True)
    work = rd / f"work_{n:02d}"
    work.mkdir(exist_ok=True)
    pad = 3.0
    a, b = max(0, c["_s"] - pad), c["_e"] + 1
    if local_src is not None:
        a = src_start
    src = Path(local_src) if local_src is not None else next(work.glob("src.*"), None)
    if src and (work / "src_start.txt").exists():
        a = float((work / "src_start.txt").read_text())
    if not src and any(k in (info.get("platform") or "").lower() for k in ("twitch", "kick")):
        got = twitch_section(info["webpage_url"], cookies, a, b, work)
        if got is not None:
            src, a = got
            (work / "src_start.txt").write_text(str(a))
    if not src:
        r = run(YTDLP + cookie_args(cookies) + ["--no-playlist", "-f",
                "bv*[height<=1080][fps<=60]+ba/b[height<=1080]/bv*+ba/b", "--download-sections", f"*{a:.1f}-{b:.1f}",
                "--merge-output-format", "mp4", "-o", str(work / "src.%(ext)s"), info["webpage_url"]])
        src = next((p for p in work.glob("src.*") if p.suffix in (".mp4", ".mkv", ".webm")), None)
        if not src:
            raise RuntimeError("section download failed: " + r.stderr[-300:])
    pr = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=width,height:format=duration", "-of", "json", str(src)]).stdout)
    W, H = pr["streams"][0]["width"], pr["streams"][0]["height"]
    sdur = float(pr["format"]["duration"])
    ss = min(c["_s"] - a, max(0, sdur - 5))
    dur = min(c["_e"] - c["_s"], sdur - ss)

    box = c.get("facecam_box")  # [left, top, width, height] in source pixels, when detection picks the wrong face
    fc = {"mode": "facecam", "box": [int(v) for v in box]} if box else find_facecam(src, W, H)
    forced = c.get("layout")  # per-pick override when the detector gets it wrong (scene switches mid-clip)
    if forced == "gameplay" or (forced == "cam" and "face" not in fc):
        fc = {"mode": "gameplay"}
    elif forced == "cam":
        fc = {"mode": "cam", "face": fc["face"]}
    words = clip_words(src, ss, dur, work)
    if fc["mode"] == "facecam":
        L, T, cw, ch = fc["box"]
        top_h = int(min(960, max(560, 1080 * ch / cw)))
        top_h -= top_h % 2
        bot_h = 1920 - top_h
        gw, gh = H * 1080 / bot_h, H
        if gw > W:
            gw, gh = W, W * bot_h / 1080
        gx, gy = (W - gw) / 2, (H - gh) / 2
        vf = (f"[0:v]fps=30,split[a][b];"
              f"[a]crop={cw}:{ch}:{L}:{T},scale=1080:{top_h}:force_original_aspect_ratio=increase,crop=1080:{top_h}[top];"
              f"[b]crop={int(gw)}:{int(gh)}:{int(gx)}:{int(gy)},scale=1080:{bot_h}[bot];[top][bot]vstack[base]")
        seam, cap_y = top_h, top_h + int(bot_h * 0.58)
    elif fc["mode"] == "cam":
        fx, fy, fw, fh = fc["face"]
        cw = min(W, H * 9 / 16)
        cx = min(max(0, fx + fw / 2 - cw / 2), W - cw)
        vf = f"[0:v]fps=30,crop={int(cw)}:{H}:{int(cx)}:0,scale=1080:1920[base]"
        seam, cap_y = 300, 1380
    else:  # no facecam: full frame in the middle over a blurred fill
        vf = ("[0:v]fps=30,split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=30:3,eq=brightness=-0.15[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2[base]")
        seam, cap_y = 560, 1420
    lst = caption_frames(words, c.get("hook") or c.get("title"), seam, cap_y, dur, work / "cap")
    out = clips / f"clip_{n:02d}_{slug(c.get('title') or c.get('hook'), 40)}.mp4"
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-f", "concat", "-safe", "0", "-i", str(lst), "-filter_complex",
             vf + ";[1:v]format=rgba[ov];[base][ov]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p[v];"
             "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]",
             "-map", "[v]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    if r.returncode != 0:
        raise RuntimeError("render failed: " + r.stderr[-500:])
    tags = " ".join(c.get("hashtags") or ["#GTA6", "#GTAVI", "#gta6clips"])
    post = (f"TITLE: {c.get('title', '')}\n\nCAPTION:\n{c.get('caption', '')}\n\n{tags}\n\n"
            f"Source: {info['streamer']}, {info['webpage_url']} at {hms(c['_s'])}\n"
            f"Category: {c.get('category')}  Score: {c.get('score')}  Layout: {fc['mode']}\n")
    out.with_suffix(".txt").write_text(post)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
        if local_src is not None:
            Path(local_src).unlink(missing_ok=True)
    return out, fc["mode"]


def cmd_render(a):
    rd = Path(a.run)
    info = json.loads((rd / "stream.json").read_text())
    t0 = time.time()
    picks, total = select(rd, a.top, a.min_score, a.max_sec)
    log(f"[{info['streamer']}] {total} possible moments -> keeping best {len(picks)}")
    made = []
    for n, c in enumerate(picks, 1):
        try:
            out, mode = render_one(rd, info, c, n, a.cookies or info.get("cookies"), a.keep)
            made.append({"file": str(out), "title": c.get("title"), "score": c.get("score"), "layout": mode,
                         "at": hms(c["_s"]), "len": round(c["_e"] - c["_s"])})
            log(f"  clip {n}: {c.get('title')}  ({hms(c['_s'])}, {round(c['_e'] - c['_s'])}s, {mode}) -> {out.name}")
        except Exception as e:
            log(f"  clip {n} FAILED: {e}")
    tm = json.loads((rd / "timing.json").read_text()) if (rd / "timing.json").exists() else {}
    tm["render_sec"] = round(time.time() - t0)
    tm["total_sec"] = round(time.time() - tm.get("prep_started", t0))
    (rd / "timing.json").write_text(json.dumps(tm, indent=1))
    summary = {"stream": info["title"], "streamer": info["streamer"], "length": hms(info.get("duration") or 0),
               "moments_found": total, "clips": made, "total_time": hms(tm["total_sec"])}
    (rd / "clips" / "summary.json").write_text(json.dumps(summary, indent=1))
    log(f"\n{info['streamer']}: {summary['length']} stream -> {total} moments -> {len(made)} clips "
        f"(total {summary['total_time']}) -> {rd / 'clips'}")


# ---------------------------------------------------------------- daily shorts
def cmd_daily(a):
    """Newest GTA 6 archive of each channel that started within --hours, longest first, then prep them."""
    now, found = time.time(), []
    for ch in [c.strip().lower() for c in a.channels.split(",") if c.strip()]:
        site, _, login = ch.rpartition(":")
        site = site or "twitch"
        listing = (f"https://kick.com/{login}/videos" if site == "kick"
                   else f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time")
        try:
            r = run(YTDLP + ["-I", "1:2", "--print", "%(id)s|%(timestamp)s|%(duration)s|%(webpage_url)s|%(title)s",
                             listing], timeout=180)
        except subprocess.TimeoutExpired:
            log(f"  {ch}: timed out")
            continue
        for ln in r.stdout.strip().splitlines():
            try:
                vid, ts, dur, page, title = ln.split("|", 4)
                ts, dur = float(ts), float(dur)
            except ValueError:
                continue
            if now - ts > a.hours * 3600 or dur < 45 * 60 or not GTA_RE.search(title):
                continue
            found.append({"channel": ch, "url": page.split("#", 1)[0],
                          "duration": dur, "title": title})
            break  # one stream per channel
        log(f"  {ch}: {'GTA stream found' if found and found[-1]['channel'] == ch else 'nothing new'}")
    found.sort(key=lambda f: -f["duration"])
    found = found[:a.max_streams]
    if not found:
        log("No new GTA 6 streams in the window.")
        return
    if a.dry_run:
        for f in found:
            log(f"  would prep {f['channel']}: {f['url']}  {hms(f['duration'])}  {f['title'][:60]}")
        return
    log(f"Prepping {len(found)}: " + ", ".join(f"{f['channel']} ({hms(f['duration'])})" for f in found))
    RUNS.mkdir(parents=True, exist_ok=True)
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(prep_one, f["url"], None, False, a.quick, False): f for f in found}
        for fu in cf.as_completed(futs):
            try:
                r = fu.result()
                log(f"RUN {r.get('run')}")
            except Exception as e:
                log(f"FAILED {futs[fu]['url']}: {e}")


def cmd_schedule(a):
    """Rank every rendered clip of the given runs by score and lay out the day's posting slots."""
    clips = []
    for rd in map(Path, a.runs):
        sm = rd / "clips" / "summary.json"
        if not sm.exists():
            continue
        info = json.loads(sm.read_text())
        for c in info.get("clips", []):
            if Path(c["file"]).exists():
                clips.append({**c, "streamer": info["streamer"]})
    clips.sort(key=lambda c: -float(c.get("score") or 0))
    slots = [x.strip() for x in a.slots.split(",")]
    out = Path(a.out) if a.out else RUNS / "shorts" / str(dt.date.today())
    out.mkdir(parents=True, exist_ok=True)
    # spread streamers: never two clips of the same streamer in one slot when avoidable
    plan = {sl: [] for sl in slots}
    for c in clips[:len(slots) * a.per_slot]:
        free = [sl for sl in slots if len(plan[sl]) < a.per_slot]
        best = min(free, key=lambda sl: (sum(x["streamer"] == c["streamer"] for x in plan[sl]), slots.index(sl)))
        plan[best].append(c)
    lines = [f"# Shorts {dt.date.today()}", ""]
    for sl in slots:
        lines.append(f"## {sl}")
        for i, c in enumerate(plan[sl], 1):
            src = Path(c["file"])
            # the file name is the Short's title: YouTube uses it as the default title on upload,
            # so the publish run can find each video and its slot without any other record
            title = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", c.get("title") or src.stem)).strip()[:90]
            dst = out / f"{sl.replace(':', '')} · {title}.mp4"
            shutil.copy2(src, dst)
            txt = src.with_suffix(".txt")
            if txt.exists():
                shutil.copy2(txt, dst.with_suffix(".txt"))
            lines.append(f"- {dst.name}  [{c['streamer']}, score {c.get('score')}, {c.get('len')} s]")
            if txt.exists():
                lines.append("  " + txt.read_text().replace("\n", "\n  ").strip())
        lines.append("")
    (out / "schedule.md").write_text("\n".join(lines))
    log("\n".join(lines))
    log(f"-> {out}")


# ---------------------------------------------------------------- main
def main():
    if not (SKILL / ".ready").exists():
        sys.exit("Not set up yet. Run: bash scripts/setup.sh")
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("discover"); d.add_argument("--limit", type=int, default=7)
    pr = sp.add_parser("prep"); pr.add_argument("urls", nargs="+")
    pr.add_argument("--cookies", help="chrome / safari / firefox, or a cookies.txt path (age-restricted YouTube, Kick)")
    pr.add_argument("--jobs", type=int, default=2, help="streams processed at once (2 is safe on a 16 GB Mac)")
    pr.add_argument("--force", action="store_true")
    pr.add_argument("--quick", type=int, default=0, metavar="N",
                    help="fast mode: skip the full transcript, only transcribe around the N loudest moments")
    pr.add_argument("--chat", action="store_true",
                    help="with --quick on a Twitch VOD: pick the moments where the chat replay explodes")
    s = sp.add_parser("status"); s.add_argument("run")
    r = sp.add_parser("render"); r.add_argument("run")
    r.add_argument("--top", type=int, default=3); r.add_argument("--min-score", type=float, default=7.0)
    r.add_argument("--cookies"); r.add_argument("--keep", action="store_true", help="keep downloaded source sections")
    r.add_argument("--max-sec", type=float, default=75, help="longest clip in seconds (59 for Shorts)")
    dl = sp.add_parser("daily", help="find yesterday's GTA 6 Twitch streams of a channel list and prep them")
    dl.add_argument("--channels", required=True, help="comma-separated Twitch logins; prefix kick: for Kick")
    dl.add_argument("--hours", type=float, default=30, help="only streams that started in the last N hours")
    dl.add_argument("--max-streams", type=int, default=4)
    dl.add_argument("--quick", type=int, default=15)
    dl.add_argument("--jobs", type=int, default=2)
    dl.add_argument("--dry-run", action="store_true", help="only list the streams that would be prepped")
    sc = sp.add_parser("schedule", help="pick the best rendered clips of several runs and assign posting slots")
    sc.add_argument("runs", nargs="+")
    sc.add_argument("--slots", default="09:00,15:00,21:00")
    sc.add_argument("--per-slot", type=int, default=3)
    sc.add_argument("--out", help="folder for the day's shorts (default ~/GTA6-Clips/shorts/<date>)")
    a = p.parse_args()
    {"discover": cmd_discover, "prep": cmd_prep, "status": cmd_status, "render": cmd_render,
     "daily": cmd_daily, "schedule": cmd_schedule}[a.cmd](a)


if __name__ == "__main__":
    main()
