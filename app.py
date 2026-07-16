import os
import re
import time
import uuid
import shutil
import zipfile
import threading
import flask
from flask import Flask, request, jsonify, send_file, render_template_string

import yt_dlp

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory job status tracker: {job_id: {"status": ..., "message": ..., "zip_path": ...}}
JOBS = {}
# Per-job cancel signal: {job_id: threading.Event}. Set by /cancel/<job_id>;
# checked inside each download's progress hook so yt-dlp stops mid-download
# rather than only being ignored once the job finishes on its own.
CANCEL_EVENTS = {}

# How long a finished file is kept on disk if the user never clicks Download.
STALE_FILE_MAX_AGE_SECONDS = 60 * 60  # 1 hour
CLEANUP_INTERVAL_SECONDS = 10 * 60    # check every 10 minutes


def _cleanup_stale_downloads():
    """Background loop: deletes finished files in DOWNLOAD_DIR that are
    older than STALE_FILE_MAX_AGE_SECONDS and were never picked up by
    /download/<job_id> (that route deletes its own file immediately after
    sending it, so this only catches abandoned/never-downloaded ones)."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            now = time.time()
            for fname in os.listdir(DOWNLOAD_DIR):
                fpath = os.path.join(DOWNLOAD_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                age = now - os.path.getmtime(fpath)
                if age > STALE_FILE_MAX_AGE_SECONDS:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        except Exception:
            # Never let the cleanup loop die from an unexpected error.
            pass


threading.Thread(target=_cleanup_stale_downloads, daemon=True).start()

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>YouTube Playlist Downloader</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

  * { box-sizing: border-box; }

  body {
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
    background: radial-gradient(circle at 15% 20%, rgba(59,130,246,0.10), transparent 40%),
                radial-gradient(circle at 85% 80%, rgba(168,85,247,0.10), transparent 40%),
                #0a0b10;
    color: #eee;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    min-height: 100vh;
    padding: 48px 20px;
    margin: 0;
  }

  .card {
    background: linear-gradient(180deg, #171922 0%, #12141b 100%);
    border: 1px solid rgba(255,255,255,0.06);
    padding: 32px;
    border-radius: 18px;
    width: 560px;
    max-width: 100%;
    box-shadow: 0 20px 60px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.02) inset;
  }

  .header-row { display: flex; align-items: center; gap: 14px; margin-bottom: 24px; }

  .logo-box {
    width: 44px; height: 44px; flex: 0 0 44px;
    border-radius: 12px;
    background: linear-gradient(135deg, #ff4d4d 0%, #ff8a3d 100%);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px rgba(255,77,77,0.35);
  }
  .logo-box svg { width: 20px; height: 20px; }

  h1 { font-size: 19px; font-weight: 700; color: #fff; margin: 0 0 2px 0; }
  .subtitle { font-size: 12.5px; color: #8a8f9c; margin: 0; }

  .field-wrap { position: relative; margin-bottom: 14px; }
  .field-icon {
    position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
    width: 17px; height: 17px; color: #a78bfa; pointer-events: none;
  }

  input[type=text] {
    width: 100%; padding: 13px 14px 13px 40px; border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
    background: #0d0f15; color: #fff; font-size: 14px; font-family: inherit;
    outline: none; transition: border-color 0.15s ease;
  }
  input[type=text]::placeholder { color: #5b606c; }
  input[type=text]:focus { border-color: #6d5efc; }

  .select-wrap { position: relative; margin-bottom: 6px; }
  .quality-badge {
    position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
    background: rgba(45,212,191,0.15); color: #2dd4bf;
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.03em;
    padding: 3px 7px; border-radius: 5px; pointer-events: none;
  }
  .select-chevron {
    position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
    width: 14px; height: 14px; color: #6b7280; pointer-events: none;
  }

  select {
    width: 100%; padding: 13px 38px 13px 56px; border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
    background: #0d0f15; color: #fff; font-size: 14px; font-family: inherit; font-weight: 600;
    outline: none; appearance: none; -webkit-appearance: none; cursor: pointer;
  }

  .hint { font-size: 12px; color: #6b7280; margin: 0 0 16px 2px; }

  button#startBtn {
    width: 100%; padding: 14px; border: none; border-radius: 10px;
    background: linear-gradient(90deg, #ef233c 0%, #ff8a3d 100%);
    color: white; font-weight: 700; cursor: pointer; font-size: 15px; font-family: inherit;
    display: flex; align-items: center; justify-content: center; gap: 8px;
    box-shadow: 0 8px 22px rgba(239,35,60,0.30);
    transition: filter 0.15s ease, transform 0.1s ease;
  }
  button#startBtn:hover:not(:disabled) { filter: brightness(1.08); }
  button#startBtn:active:not(:disabled) { transform: translateY(1px); }
  button#startBtn:disabled { background: #3a3d46; box-shadow: none; cursor: not-allowed; }
  button#startBtn svg { width: 17px; height: 17px; }

  #progressSection {
    display: none; margin-top: 20px; padding-top: 18px;
    border-top: 1px solid rgba(255,255,255,0.06);
  }
  .progress-top-row { display: flex; align-items: center; gap: 12px; }
  .status-icon {
    width: 34px; height: 34px; flex: 0 0 34px; border-radius: 50%;
    border: 2px solid #2dd4bf; display: flex; align-items: center; justify-content: center;
    color: #2dd4bf;
  }
  .status-icon svg { width: 16px; height: 16px; }
  .progress-text-col { flex: 1; min-width: 0; }
  .progress-label { font-size: 14px; font-weight: 700; color: #fff; margin: 0 0 8px 0; }
  .progress-pct {
    position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
    font-size: 10px; font-weight: 800; color: #fff;
    text-shadow: 0 1px 2px rgba(0,0,0,0.85);
    letter-spacing: 0.02em; pointer-events: none; z-index: 2;
  }
  .progress-track {
    position: relative;
    width: 100%; height: 14px; border-radius: 999px;
    background: transparent;
    border: 1.5px solid rgba(45,212,191,0.35);
    box-shadow: 0 0 8px rgba(45,212,191,0.25), inset 0 0 6px rgba(45,212,191,0.12);
    overflow: hidden;
  }
  .progress-fill {
    position: relative;
    height: 100%; border-radius: 999px; width: 0%;
    background:
      repeating-linear-gradient(
        45deg,
        rgba(15,17,23,0.9) 0px,
        rgba(15,17,23,0.9) 6px,
        #2dd4bf 6px,
        #22d3ee 12px
      );
    background-size: 200% 100%;
    box-shadow: 0 0 12px rgba(45,212,191,0.85), 0 0 22px rgba(34,211,238,0.5);
    transition: width 0.4s ease;
    animation: stripeMove 1.1s linear infinite;
  }
  .progress-fill::after {
    content: '';
    position: absolute; top: 50%; right: -1px; transform: translate(50%, -50%);
    width: 12px; height: 12px; border-radius: 50%;
    background: #e0fffb;
    box-shadow: 0 0 6px 2px #ffffff, 0 0 14px 4px rgba(45,212,191,0.9);
    animation: dotPulse 1.3s ease-in-out infinite;
  }
  @keyframes stripeMove {
    from { background-position: 0 0; }
    to { background-position: -34px 0; }
  }
  @keyframes dotPulse {
    0%, 100% { opacity: 1; transform: translate(50%, -50%) scale(1); }
    50% { opacity: 0.75; transform: translate(50%, -50%) scale(1.15); }
  }
  #status {
    margin-top: 10px; font-size: 12.5px; color: #8a8f9c; white-space: pre-line;
  }

  .cancel-row { display: flex; justify-content: flex-end; margin-top: 10px; padding-right: 2px; }

  button#cancelBtn {
    display: none;
    flex: 0 0 auto;
    padding: 6px 12px; border-radius: 8px;
    border: none;
    background: linear-gradient(180deg, #ff5f5f 0%, #e11d1d 55%, #c11414 100%);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.5), inset 0 -4px 8px rgba(0,0,0,0.25), 0 3px 8px rgba(225,29,29,0.35);
    color: white; font-weight: 700; font-size: 11.5px; font-family: inherit;
    letter-spacing: 0.02em; text-transform: uppercase; cursor: pointer;
    display: none; align-items: center; gap: 5px;
    transition: filter 0.15s ease, transform 0.1s ease;
  }
  button#cancelBtn svg { width: 11px; height: 11px; }
  button#cancelBtn:hover:not(:disabled) { filter: brightness(1.1); }
  button#cancelBtn:active:not(:disabled) { transform: translateY(1px); }
  button#cancelBtn:disabled { opacity: 0.5; cursor: not-allowed; }

  #downloadLink { display: none; margin-top: 16px; }
  #downloadLink a {
    display: block; text-align: center; padding: 12px; border-radius: 10px;
    background: rgba(45,212,191,0.12); border: 1px solid rgba(45,212,191,0.35);
    color: #2dd4bf; text-decoration: none; font-weight: 700; font-size: 14px;
  }

  .features-row {
    display: flex; gap: 18px; margin-top: 22px; padding-top: 18px;
    border-top: 1px solid rgba(255,255,255,0.06);
  }
  .feature { display: flex; align-items: flex-start; gap: 8px; flex: 1; min-width: 0; }
  .feature-icon {
    width: 26px; height: 26px; flex: 0 0 26px; border-radius: 8px;
    display: flex; align-items: center; justify-content: center; margin-top: 1px;
  }
  .feature-icon svg { width: 13px; height: 13px; }
  .feature-title { font-size: 12px; font-weight: 700; color: #fff; margin: 0; }
  .feature-sub { font-size: 10.5px; color: #6b7280; margin: 1px 0 0 0; }

  @media (max-width: 480px) {
    .features-row { flex-direction: column; gap: 12px; }
  }

  .credit-footer {
    text-align: center;
    font-size: 11px;
    color: #6b7280;
    margin: 26px 0 4px 0;
    letter-spacing: 0.01em;
  }
  .credit-footer span { color: #d1d5db; font-weight: 600; }
</style>
</head>
<body>
<div class="card">
  <div class="header-row">
    <div class="logo-box">
      <svg viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
    </div>
    <div>
      <h1>YouTube Playlist Downloader</h1>
      <p class="subtitle">Download entire YouTube playlists in high quality</p>
    </div>
  </div>

  <div class="field-wrap">
    <svg class="field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
    <input type="text" id="url" placeholder="Paste playlist or video link" onblur="checkFormats()">
  </div>

  <div id="qualityHint" class="hint"></div>

  <div class="select-wrap">
    <span class="quality-badge">HD</span>
    <select id="quality"></select>
    <svg class="select-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
  </div>

  <div class="hint">Tip: "Fast" downloads quicker and works in any player.</div>

  <button id="startBtn" onclick="startDownload()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    Start Download
  </button>

  <div id="progressSection">
    <div class="progress-top-row">
      <div class="status-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <div class="progress-text-col">
        <p class="progress-label" id="progressLabel">Downloading...</p>
        <div class="progress-track">
          <div class="progress-fill" id="progressFill"></div>
          <span class="progress-pct" id="progressPct"></span>
        </div>
      </div>
    </div>
    <div id="status"></div>
    <div class="cancel-row">
      <button id="cancelBtn" onclick="cancelDownload()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        <span id="cancelBtnLabel">Cancel</span>
      </button>
    </div>
  </div>

  <div id="downloadLink"></div>

  <div class="features-row">
    <div class="feature">
      <div class="feature-icon" style="background: rgba(96,165,250,0.15); color:#60a5fa;">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg>
      </div>
      <div><p class="feature-title">High Speed</p><p class="feature-sub">Ultra-fast downloads</p></div>
    </div>
    <div class="feature">
      <div class="feature-icon" style="background: rgba(168,85,247,0.15); color:#a78bfa;">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 4 5v6c0 5.5 3.8 10.7 8 12 4.2-1.3 8-6.5 8-12V5l-8-3z"/></svg>
      </div>
      <div><p class="feature-title">Safe &amp; Secure</p><p class="feature-sub">No data collection</p></div>
    </div>
    <div class="feature">
      <div class="feature-icon" style="background: rgba(45,212,191,0.15); color:#2dd4bf;">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="16 9 10.5 15 8 12.5"/></svg>
      </div>
      <div><p class="feature-title">Complete</p><p class="feature-sub">All videos included</p></div>
    </div>
  </div>

  <p class="credit-footer">Designed by <span>Shivank Thakur</span></p>
</div>

<script>
let pollTimer = null;
let currentJobId = null;

const FIXED_OPTIONS = [
  { value: "fast", label: "Fast (480p, smaller & quicker)" },
  { value: "720", label: "720p (balanced)" },
  { value: "1080", label: "1080p (high quality, slower)" },
  { value: "best", label: "Best Quality (largest, slowest)" },
  { value: "audio", label: "Audio Only (MP3)" },
];

function renderOptions(options) {
  const sel = document.getElementById('quality');
  sel.innerHTML = '';
  for (const opt of options) {
    const el = document.createElement('option');
    el.value = opt.value;
    el.textContent = opt.label;
    sel.appendChild(el);
  }
}

// Human-friendly labels for common heights; anything else falls back to "Np".
const HEIGHT_LABELS = {
  480: "480p", 720: "720p (HD)", 1080: "1080p (Full HD)",
  1440: "1440p (2K)", 2160: "2160p (4K)",
};

let lastCheckedUrl = "";

async function checkFormats() {
  const url = document.getElementById('url').value.trim();
  const hint = document.getElementById('qualityHint');
  if (!url || url === lastCheckedUrl) {
    if (!url) { renderOptions(FIXED_OPTIONS); hint.innerText = ''; }
    return;
  }
  lastCheckedUrl = url;

  hint.innerText = 'Checking available qualities...';
  try {
    const res = await fetch('/check-formats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();

    if (data.error || data.is_playlist) {
      // Playlist, or couldn't be read as a single video: use the fixed list.
      renderOptions(FIXED_OPTIONS);
      hint.innerText = data.is_playlist
        ? 'Playlist detected — using standard quality options.'
        : '';
      return;
    }

    const heights = (data.available_heights || []).filter(h => h >= 360);
    const vp9Heights = (data.vp9_heights || []).filter(h => h >= 360);
    if (heights.length === 0 && vp9Heights.length === 0) {
      renderOptions(FIXED_OPTIONS);
      hint.innerText = '';
      return;
    }

    // H.264 entries: play in any player. Listed with their plain height as
    // the value (e.g. "1080"), matching the existing /start behavior.
    const h264Options = heights.slice().reverse().map(h => ({
      value: String(h),
      label: (HEIGHT_LABELS[h] || (h + 'p')) + ' — Universal',
    }));

    // VP9/AV1-only entries (1440p/4K etc. that don't exist in H.264 for this
    // video): value carries a "-vp9" suffix so /start knows to allow
    // VP9/AV1 instead of requiring avc1.
    const vp9Options = vp9Heights.slice().reverse().map(h => ({
      value: String(h) + '-vp9',
      label: (HEIGHT_LABELS[h] || (h + 'p')) + ' — 4K/VP9 (modern players)',
    }));

    // Build options only for resolutions that actually exist for this video,
    // highest first, plus Audio Only at the end.
    const dynamic = [...vp9Options, ...h264Options];
    dynamic.push({ value: "audio", label: "Audio Only (MP3)" });
    renderOptions(dynamic);
    hint.innerText = 'Showing qualities available for this video.';
  } catch (e) {
    renderOptions(FIXED_OPTIONS);
    hint.innerText = '';
  }
}

renderOptions(FIXED_OPTIONS);

async function startDownload() {
  const url = document.getElementById('url').value.trim();
  const quality = document.getElementById('quality').value;
  const btn = document.getElementById('startBtn');
  const status = document.getElementById('status');
  const dl = document.getElementById('downloadLink');
  const progressSection = document.getElementById('progressSection');
  const progressFill = document.getElementById('progressFill');
  const progressPct = document.getElementById('progressPct');
  const progressLabel = document.getElementById('progressLabel');
  const cancelBtn = document.getElementById('cancelBtn');
  dl.style.display = 'none';
  dl.innerHTML = '';

  if (!url) {
    status.innerText = "Please paste a link first.";
    return;
  }

  btn.disabled = true;
  progressSection.style.display = 'block';
  progressFill.style.width = '0%';
  progressPct.innerText = '';
  progressLabel.innerText = 'Starting...';
  status.innerText = '';
  cancelBtn.style.display = 'flex';
  cancelBtn.disabled = false;
  document.getElementById('cancelBtnLabel').innerText = 'Cancel';

  const res = await fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, quality})
  });
  const data = await res.json();

  if (data.error) {
    progressLabel.innerText = "Error";
    status.innerText = data.error;
    btn.disabled = false;
    cancelBtn.style.display = 'none';
    return;
  }

  currentJobId = data.job_id;
  pollTimer = setInterval(() => checkStatus(currentJobId), 2000);
}

async function cancelDownload() {
  if (!currentJobId) return;
  const cancelBtn = document.getElementById('cancelBtn');
  const progressLabel = document.getElementById('progressLabel');
  cancelBtn.disabled = true;
  document.getElementById('cancelBtnLabel').innerText = 'Wait...';
  progressLabel.innerText = 'Cancelling...';
  try {
    await fetch('/cancel/' + currentJobId, { method: 'POST' });
  } catch (e) {
    // Status polling will still pick up the final state either way.
  }
}

async function checkStatus(jobId) {
  const status = document.getElementById('status');
  const dl = document.getElementById('downloadLink');
  const btn = document.getElementById('startBtn');
  const progressFill = document.getElementById('progressFill');
  const progressPct = document.getElementById('progressPct');
  const progressLabel = document.getElementById('progressLabel');
  const cancelBtn = document.getElementById('cancelBtn');

  const res = await fetch('/status/' + jobId);
  const data = await res.json();
  const p = data.progress;

  if (p) {
    const pct = Math.min(100, p.percent || 0);
    progressFill.style.width = pct + '%';
    progressPct.innerText = Math.round(pct) + '%';
    status.innerText =
      p.videos_done + ' / ' + p.videos_total + ' videos  •  ' +
      p.downloaded_str + ' / ' + p.total_str + '  •  ' +
      p.speed_mbps + ' MB/s';
  } else {
    status.innerText = data.message || data.status || '';
  }

  progressLabel.innerText = data.status === 'downloading' ? 'Downloading...' : (data.message || data.status);

  if (data.status === 'done') {
    clearInterval(pollTimer);
    btn.disabled = false;
    cancelBtn.style.display = 'none';
    progressFill.style.width = '100%';
    progressPct.innerText = '100%';
    progressLabel.innerText = 'Complete';
    dl.style.display = 'block';
    const linkText = data.is_zip ? '✅ Download your ZIP file' : '✅ Download your video';
    dl.innerHTML = '<a href="/download/' + jobId + '">' + linkText + '</a>';
  } else if (data.status === 'error') {
    clearInterval(pollTimer);
    btn.disabled = false;
    cancelBtn.style.display = 'none';
    progressLabel.innerText = 'Error';
  } else if (data.status === 'cancelled') {
    clearInterval(pollTimer);
    btn.disabled = false;
    cancelBtn.style.display = 'none';
    progressLabel.innerText = 'Cancelled';
    status.innerText = 'Download cancelled. You can change the quality and start again.';
  }
}
</script>
</body>
</html>
"""


def run_download_job(job_id, url, quality):
    job_folder = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_folder, exist_ok=True)

    cancel_event = threading.Event()
    CANCEL_EVENTS[job_id] = cancel_event

    JOBS[job_id]["status"] = "downloading"
    JOBS[job_id]["message"] = "Starting..."

    # No forced re-encoding: video and audio streams are downloaded at their
    # original YouTube quality and merged as-is (-c copy for both streams),
    # so codec/bitrate/sample-rate stay exactly what YouTube served.
    no_reencode_args = {
        "postprocessor_args": {
            "ffmpeg": ["-c:v", "copy", "-c:a", "copy"]
        }
    }

    if quality == "audio":
        fmt = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif quality == "fast":
        # Strict H.264 (avc1): height<=480 + vcodec^=avc1 picks the best H.264
        # stream at or below 480p. No non-H.264 fallback, so the file is
        # always playable in Windows Media Player / Movies & TV / any device.
        fmt = (
            "bestvideo[height<=480][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=480][vcodec^=avc1]+bestaudio"
        )
        postprocessors = []
    elif quality == "best":
        fmt = (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio"
        )
        postprocessors = []
    elif quality.isdigit():
        # Covers the fixed "720"/"1080" options as well as any dynamically
        # detected height (e.g. "1440", "2160") coming from /check-formats.
        # height<=H + vcodec^=avc1 (strict H.264) means yt-dlp automatically
        # steps down to the highest H.264 stream available at or below the
        # requested height (e.g. request 1440p, get 1080p H.264) rather than
        # ever falling back to a VP9/AV1 stream at the exact requested height.
        h = quality
        fmt = (
            f"bestvideo[height<={h}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio"
        )
        postprocessors = []
    elif quality.endswith("-vp9") and quality[:-4].isdigit():
        # 1440p/4K option from the dropdown: these heights only exist in
        # VP9/AV1 on YouTube, so avc1 is intentionally not required here.
        # Plays fine in modern players (VLC, browsers, phones) but is not
        # guaranteed on very old/basic devices — that tradeoff is why this
        # is offered as a separate dropdown entry instead of replacing the
        # H.264 path above.
        h = quality[:-4]
        fmt = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}]"
        )
        postprocessors = []
    else:
        fmt = (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio"
        )
        postprocessors = []

    base_ydl_opts = {
        "format": fmt,
        "outtmpl": os.path.join(job_folder, "%(playlist_index)s - %(title)s.%(ext)s"),
        "ignoreerrors": True,
        "noplaylist": False,
        "postprocessors": postprocessors,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            },
            "youtubepot-bgutilhttp": {},
        },
        "remote_components": ["ejs:github"],
    }
    if quality != "audio":
        base_ydl_opts.update(no_reencode_args)

    # Progress tracking across parallel workers
    progress_lock = threading.Lock()
    video_progress = {}  # video index -> dict of raw progress numbers

    def format_bytes(n):
        if not n:
            return "0 MB"
        mb = n / (1024 * 1024)
        if mb >= 1024:
            return f"{mb / 1024:.2f} GB"
        return f"{mb:.0f} MB"

    def recompute_aggregate(total_videos):
        # Sum downloaded/total bytes and speed across all videos currently
        # being tracked, so the UI can show one combined "X MB / Y MB • Z MB/s"
        # line instead of a raw per-video dump.
        downloaded = sum(v.get("downloaded_bytes", 0) for v in video_progress.values())
        total = sum(v.get("total_bytes", 0) for v in video_progress.values())
        speed = sum(v.get("speed", 0) or 0 for v in video_progress.values())
        finished_count = sum(1 for v in video_progress.values() if v.get("status") == "finished")
        pct = round((downloaded / total) * 100, 1) if total else 0.0

        JOBS[job_id]["progress"] = {
            "percent": pct,
            "downloaded_mb": round(downloaded / (1024 * 1024), 1),
            "total_mb": round(total / (1024 * 1024), 1),
            "downloaded_str": format_bytes(downloaded),
            "total_str": format_bytes(total),
            "speed_mbps": round((speed / (1024 * 1024)), 2) if speed else 0,
            "videos_done": finished_count,
            "videos_total": total_videos,
        }
        JOBS[job_id]["message"] = (
            f"{finished_count}/{total_videos} videos • "
            f"{format_bytes(downloaded)} / {format_bytes(total)} • "
            f"{round(speed / (1024 * 1024), 1) if speed else 0} MB/s"
        )

    actual_heights = {}  # label -> actual downloaded height (int), for detecting downgrades
    # A video downloaded as separate video-only + audio-only streams calls
    # the progress hook once per stream with d["status"] == "downloading".
    # Track each stream's bytes under its own sub-key (label + format id),
    # then sum the sub-streams into one combined entry per video, so the
    # audio stream's small total doesn't overwrite the video stream's larger
    # total (this was the cause of e.g. "4 MB / 4 MB" being shown for a
    # 200 MB video).
    substream_progress = {}  # (label, format_id) -> dict of raw progress numbers

    def make_progress_hook(label, index, total_videos):
        def hook(d):
            if cancel_event.is_set():
                # Raising this inside a progress hook is yt-dlp's supported
                # way to abort an in-progress download from the outside —
                # it stops the current fragment/stream immediately instead
                # of waiting for it to finish on its own.
                raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
            with progress_lock:
                info = d.get("info_dict") or {}
                h = info.get("height")
                if h:
                    actual_heights[label] = int(h)
                format_id = info.get("format_id") or d.get("format_id") or "default"
                sub_key = (label, format_id)

                if d["status"] == "downloading":
                    substream_progress[sub_key] = {
                        "status": "downloading",
                        "downloaded_bytes": d.get("downloaded_bytes", 0) or 0,
                        "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                        "speed": d.get("speed", 0) or 0,
                    }
                elif d["status"] == "finished":
                    prev = substream_progress.get(sub_key, {})
                    substream_progress[sub_key] = {
                        "status": "finished",
                        "downloaded_bytes": prev.get("total_bytes", 0) or prev.get("downloaded_bytes", 0),
                        "total_bytes": prev.get("total_bytes", 0) or prev.get("downloaded_bytes", 0),
                        "speed": 0,
                    }

                # Roll up all sub-streams belonging to this video into one
                # combined entry. "finished" only once every sub-stream for
                # this video has finished — otherwise a video-only stream
                # finishing early would prematurely mark the whole video done
                # while the audio-only stream is still downloading.
                own_substreams = [v for k, v in substream_progress.items() if k[0] == label]
                all_finished = all(v.get("status") == "finished" for v in own_substreams)
                video_progress[label] = {
                    "status": "finished" if all_finished else "downloading",
                    "downloaded_bytes": sum(v.get("downloaded_bytes", 0) for v in own_substreams),
                    "total_bytes": sum(v.get("total_bytes", 0) for v in own_substreams),
                    "speed": sum(v.get("speed", 0) or 0 for v in own_substreams if v.get("status") == "downloading"),
                }
                recompute_aggregate(total_videos)
        return hook

    def skip_if_live(info_dict, **kwargs):
        if info_dict.get("is_live") or info_dict.get("live_status") in ("is_live", "is_upcoming"):
            return "Skipping: this is a live video and can't be downloaded while live."
        return None

    def download_one(video_url, label, index, total_videos):
        opts = dict(base_ydl_opts)
        opts["progress_hooks"] = [make_progress_hook(label, index, total_videos)]
        opts["noplaylist"] = True
        opts["match_filter"] = skip_if_live
        opts["outtmpl"] = os.path.join(job_folder, f"{index:02d} - %(title)s.%(ext)s")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            with progress_lock:
                video_progress[label] = {"status": "failed", "downloaded_bytes": 0, "total_bytes": 0, "speed": 0}
                recompute_aggregate(total_videos)

    try:
        # Step 1: extract playlist entries (or single video) without downloading
        JOBS[job_id]["message"] = "Fetching video info..."
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": "in_playlist", "ignoreerrors": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_title = (info.get("title") if info else None) or "youtube_download"
        # Windows forbids \ / : * ? " < > | in filenames; also trim whitespace
        # and cap length so very long playlist titles don't break on save.
        safe_title = re.sub(r'[\\/:*?"<>|]', "", raw_title).strip()
        safe_title = re.sub(r"\s+", " ", safe_title)[:80] or "youtube_download"
        JOBS[job_id]["title"] = safe_title

        entries_list = list(info["entries"]) if (info and "entries" in info and info["entries"]) else []
        is_playlist = len(entries_list) > 1
        if is_playlist:
            JOBS[job_id]["message"] = "Fetching playlist info..."

        if not is_playlist and info:
            # A currently-live or not-yet-started broadcast has no fixed length
            # and would make yt-dlp/ffmpeg record forever. An already-ended
            # live stream (was_live=True, live_status == "was_live") has a
            # normal VOD file behind it and downloads fine, so it's allowed.
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["message"] = (
                    "This is a live video — it can't be downloaded while it's live. "
                    "Come back after the stream ends, or paste a regular video link."
                )
                return

        entries = []
        if info and "entries" in info and info["entries"]:
            for idx, entry in enumerate(info["entries"], start=1):
                if not entry:
                    continue
                video_id = entry.get("id")
                video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
                entries.append((idx, video_url))
        else:
            entries = [(1, url)]

        total = len(entries)
        JOBS[job_id]["message"] = f"Downloading {total} video(s), 3 at a time..."

        # Step 2: download up to 3 videos in parallel
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
        MAX_PARALLEL = 2
        PER_VIDEO_TIMEOUT = 600  # seconds; prevents one stuck video from hanging the whole job
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
            future_to_label = {}
            for idx, video_url in entries:
                label = f"Video {idx}/{total}"
                fut = executor.submit(download_one, video_url, label, idx, total)
                future_to_label[fut] = label
            for f in future_to_label:
                label = future_to_label[f]
                try:
                    f.result(timeout=PER_VIDEO_TIMEOUT)
                except FutureTimeoutError:
                    with progress_lock:
                        video_progress[label] = {"status": "failed", "downloaded_bytes": 0, "total_bytes": 0, "speed": 0}
                        recompute_aggregate(total)

        if cancel_event.is_set():
            JOBS[job_id]["status"] = "cancelled"
            JOBS[job_id]["message"] = "Download cancelled."
            shutil.rmtree(job_folder, ignore_errors=True)
            CANCEL_EVENTS.pop(job_id, None)
            return

        JOBS[job_id]["message"] = "Checking for incomplete files..."
        incomplete_prefixes = set()
        skip_files = set()
        for fname in os.listdir(job_folder):
            # Raw video/audio-only streams (e.g. "02 - Title.f137"), ffmpeg's
            # in-progress merge output ("*.temp"), or a never-finished download
            # ("*.part") all mean that video's merge/download did not complete.
            if re.search(r"\.f\d+$", fname) or fname.endswith(".temp") or fname.endswith(".part"):
                skip_files.add(fname)
                prefix_match = re.match(r"^(\d+)\s*-", fname)
                if prefix_match:
                    incomplete_prefixes.add(prefix_match.group(1))

        remaining_files = [f for f in os.listdir(job_folder) if f not in skip_files]

        def resolution_note():
            # Only meaningful when a specific height was requested; "fast"/
            # "best"/"audio" have no fixed target to compare against.
            is_vp9_request = quality.endswith("-vp9") and quality[:-4].isdigit()
            if not (quality.isdigit() or is_vp9_request) or not actual_heights:
                return ""
            requested_h = int(quality[:-4]) if is_vp9_request else int(quality)
            got_h = max(actual_heights.values())
            if got_h < requested_h:
                codec_label = "VP9/AV1" if is_vp9_request else "H.264"
                return (
                    f" Downloaded at {got_h}p ({codec_label}) — {requested_h}p isn't "
                    f"available in a compatible format for this video."
                )
            return ""

        if not is_playlist:
            # Single video: no zip. Move the one finished file straight into
            # DOWNLOAD_DIR under its title, so both disk and the download
            # button serve the actual video/audio file, not an archive.
            if not remaining_files:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["message"] = "Download didn't complete — nothing to save. Please try again."
                shutil.rmtree(job_folder, ignore_errors=True)
                return

            src_name = remaining_files[0]
            ext = os.path.splitext(src_name)[1]  # keep real extension (.mp4/.mp3/etc)
            final_filename = f"{safe_title} - {job_id[:6]}{ext}"
            final_path = os.path.join(DOWNLOAD_DIR, final_filename)
            shutil.move(os.path.join(job_folder, src_name), final_path)

            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["message"] = "Download complete! Your file is ready." + resolution_note()
            JOBS[job_id]["file_path"] = final_path
            JOBS[job_id]["is_zip"] = False
            shutil.rmtree(job_folder, ignore_errors=True)
            return

        JOBS[job_id]["message"] = "Creating ZIP file..."
        zip_filename = f"{safe_title} - {job_id[:6]}.zip"
        zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(job_folder):
                for file in files:
                    if file in skip_files:
                        continue
                    full_path = os.path.join(root, file)
                    zf.write(full_path, arcname=file)

        JOBS[job_id]["status"] = "done"
        if incomplete_prefixes:
            nums = ", ".join(sorted(incomplete_prefixes, key=lambda x: int(x)))
            JOBS[job_id]["message"] = (
                f"Done, but video(s) {nums} didn't merge properly and were left out "
                f"of the ZIP. Try re-running just those."
            )
        else:
            JOBS[job_id]["message"] = "Download complete! Your ZIP is ready." + resolution_note()
        JOBS[job_id]["file_path"] = zip_path
        JOBS[job_id]["is_zip"] = True

        shutil.rmtree(job_folder, ignore_errors=True)

    except Exception as e:
        if not cancel_event.is_set():
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["message"] = f"Something went wrong: {str(e)}"
        else:
            JOBS[job_id]["status"] = "cancelled"
            JOBS[job_id]["message"] = "Download cancelled."
        shutil.rmtree(job_folder, ignore_errors=True)
    finally:
        CANCEL_EVENTS.pop(job_id, None)


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/check-formats", methods=["POST"])
def check_formats():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url or not re.match(r"^https?://", url):
        return jsonify({"error": "Please enter a valid link (must start with http/https)."}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": "in_playlist", "ignoreerrors": True}) as ydl:
            flat_info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Could not read this link: {str(e)[:120]}"}), 400

    is_playlist = bool(flat_info and flat_info.get("entries") and len(list(flat_info["entries"])) > 1)
    if is_playlist:
        # Available resolution differs per video in a playlist, so we don't
        # try to compute a single list here — the frontend falls back to the
        # fixed quality dropdown for playlist links.
        return jsonify({"is_playlist": True})

    # Single video: fetch its actual format list (still no download).
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "ignoreerrors": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Could not read this video: {str(e)[:120]}"}), 400

    h264_heights = set()
    other_heights = set()
    for f in (info.get("formats") or []):
        h = f.get("height")
        vcodec = f.get("vcodec") or ""
        if not h:
            continue
        if vcodec.startswith("avc1"):
            # H.264 — plays in any player (Windows Media Player, older TVs,
            # any device). Matches the strict H.264-only selection used at
            # download time for these entries.
            h264_heights.add(int(h))
        elif vcodec.startswith("vp9") or vcodec.startswith("av01"):
            # VP9 / AV1 — this is where 1440p/4K actually lives on YouTube.
            # Plays fine in modern players (VLC, browsers, phones) but not
            # guaranteed on very old/basic devices.
            other_heights.add(int(h))

    # A height available in both is only listed once, as the universal
    # (H.264) option — no need to offer a duplicate VP9 entry for it.
    vp9_only_heights = other_heights - h264_heights

    return jsonify({
        "is_playlist": False,
        "available_heights": sorted(h264_heights),
        "vp9_heights": sorted(vp9_only_heights),
    })


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "fast")

    if not url or not re.match(r"^https?://", url):
        return jsonify({"error": "Please enter a valid link (must start with http/https)."}), 400

    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "queued", "message": "In queue..."}

    thread = threading.Thread(target=run_download_job, args=(job_id, url, quality))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job.get("status") in ("done", "error", "cancelled"):
        # Already finished one way or another — nothing to stop.
        return jsonify({"status": job.get("status")})

    cancel_event = CANCEL_EVENTS.get(job_id)
    if cancel_event:
        cancel_event.set()
    JOBS[job_id]["status"] = "cancelling"
    JOBS[job_id]["message"] = "Cancelling..."
    return jsonify({"status": "cancelling"})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found."}), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return "File is not ready yet.", 400
    title = job.get("title") or f"download_{job_id}"
    file_path = job.get("file_path")
    if not file_path or not os.path.exists(file_path):
        return "File is not ready yet.", 400
    ext = ".zip" if job.get("is_zip") else os.path.splitext(file_path)[1]

    @flask.after_this_request
    def _delete_after_send(response):
        # File has been handed to the user's browser — no need to keep our
        # copy anymore. Runs after the response is fully sent.
        try:
            os.remove(file_path)
        except OSError:
            pass
        return response

    return send_file(file_path, as_attachment=True, download_name=f"{title}{ext}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\nServer started! Open this link in your browser: http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
