"""
app.py — Discord-Powered Premium Cloud Storage App
-----------------------------------------------------
Flask backend that uses a Discord Webhook as a "CDN" storage backend.

Responsibilities:
  1. Accept file uploads from the frontend (single or batch, tagged with
     an upload `source` tab — gallery / folders / all — and optionally a
     folder path so files can be uploaded directly into a folder).
  2. Run the actual upload as a BACKGROUND JOB so the frontend can poll
     real, live progress — including the server-side phase of pushing
     each Discord chunk — instead of the browser's upload bar jumping to
     100% the moment the file reaches our server and then going quiet.
  3. Split any file larger than CHUNK_SIZE into sequential parts before
     sending to Discord (Discord's free-tier webhook attachment cap is
     10 MB) — each part is uploaded as its own message/attachment.
  4. Push each chunk to the Discord webhook with ?wait=true so we get
     back the message JSON (permanent CDN URL + message id, needed later
     for deletion).
  5. Extract EXIF "DateTimeOriginal" from images via Pillow and store it
     as the file's "Captured Date".
  6. Persist all of this metadata — files AND folders — in database.json
     (a tiny flat-file DB), so a chunked file can always be found and
     reassembled, and folders can exist even before/after they hold files.
  7. Support deleting a file: removes every chunk's message from Discord
     AND removes its DB entry. Also supports bulk delete.
  8. Support downloading a file: reassembles all of its chunks, in
     order, back into the single original file. Also supports bulk
     download, which reassembles several files and zips them together.
  9. Support a live PREVIEW stream (with HTTP Range support) so even a
     large file that got split into several Discord chunks can still be
     watched/viewed before it's fully downloaded.
 10. Support folders as real, independently-creatable entities (so an
     empty folder still exists and still nests properly under its
     parent), not just something inferred from file paths.

Nothing here talks to a real database — database.json is intentionally
simple so the whole project stays copy-pasteable and inspectable.
"""

import os
import io
import re
import json
import math
import time
import uuid
import zipfile
import mimetypes
import threading
from datetime import datetime

from flask import Flask, request, jsonify, render_template, Response, send_file
from werkzeug.utils import secure_filename
import requests

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
except ImportError:  # Pillow is required, but fail soft so the server still boots
    Image = None
    TAGS = {}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(APP_ROOT, "database.json")
TEMP_STORAGE = os.path.join(APP_ROOT, "temp_storage")

# Paste your Discord Webhook URL here, or set the DISCORD_WEBHOOK_URL env var.
# Example: https://discord.com/api/webhooks/1234567890/AbCdEfGhIjK...
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1531408682372567130/XzIVpFSK4V_F_k9U7gQGQgVpWboM4fgGC0mRVzejjD7-BB2ZhlxeVkb3B-FiUrSCgl-O",
)

# Discord's free-tier attachment cap for a normal webhook post is 10 MB.
# We split at a slightly smaller size to leave headroom for multipart
# overhead, so we never accidentally trip the hard limit.
CHUNK_SIZE = int(9.99 * 1_000_000)  # 9.99 MB per chunk (decimal MB), safely under Discord's 10 MB cap

os.makedirs(TEMP_STORAGE, exist_ok=True)

app = Flask(__name__)

# A simple lock so concurrent uploads don't corrupt database.json when
# several requests read-modify-write it at nearly the same time.
_db_lock = threading.Lock()

# ---------------------------------------------------------------------------
# In-memory live upload progress
# ---------------------------------------------------------------------------
# Keyed by upload_id. The frontend polls GET /api/upload/status/<id> while
# the background job (below) pushes chunks to Discord, so a large file's
# upload card can show real progress + an ETA instead of stalling at 100%
# right after the browser->server transfer finishes.
_progress_lock = threading.Lock()
UPLOAD_PROGRESS = {}
_PROGRESS_TTL_SECONDS = 30 * 60  # forget finished jobs after 30 minutes


def _purge_old_progress():
    cutoff = time.time() - _PROGRESS_TTL_SECONDS
    with _progress_lock:
        stale = [
            uid for uid, p in UPLOAD_PROGRESS.items()
            if p.get("status") in ("done", "error") and p.get("finished_at", time.time()) < cutoff
        ]
        for uid in stale:
            del UPLOAD_PROGRESS[uid]


# ---------------------------------------------------------------------------
# Flat-file "database" helpers
# ---------------------------------------------------------------------------
# database.json shape: {"files": [ ...file records... ], "folders": [ "path", ... ]}

def load_db():
    """Return the full {"files": [...], "folders": [...]} dict, creating
    or migrating the file on disk as needed."""
    if not os.path.exists(DATABASE_PATH):
        default = {"files": [], "folders": []}
        with open(DATABASE_PATH, "w") as f:
            json.dump(default, f)
        return default

    with open(DATABASE_PATH, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}

    if isinstance(data, list):  # migrate from the older "just a list" shape
        data = {"files": data, "folders": []}

    data.setdefault("files", [])
    data.setdefault("folders", [])
    return data


def save_db(data):
    with open(DATABASE_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def normalize_path(path):
    return (path or "").replace("\\", "/").strip("/")


def register_folder_path(db, path):
    """Ensure `path` and every one of its ancestor folders exist in the
    explicit folders list, so nested structure is never lost."""
    path = normalize_path(path)
    if not path:
        return
    parts = path.split("/")
    prefix = ""
    for part in parts:
        prefix = f"{prefix}/{part}" if prefix else part
        if prefix not in db["folders"]:
            db["folders"].append(prefix)


def add_file_record(record):
    with _db_lock:
        db = load_db()
        db["files"].append(record)
        register_folder_path(db, record.get("folder_path", ""))
        save_db(db)


def remove_file_record(file_id):
    with _db_lock:
        db = load_db()
        db["files"] = [r for r in db["files"] if r["id"] != file_id]
        save_db(db)


def find_file_record(file_id):
    for r in load_db()["files"]:
        if r["id"] == file_id:
            return r
    return None


def sanitize_folder_segment(segment):
    """Folder names are metadata only (never touch the real filesystem),
    so we just block path traversal and empty/blank segments."""
    segment = segment.strip().replace("/", "-").replace("\\", "-")
    if segment in ("", ".", ".."):
        return None
    return segment


def create_folder(raw_path):
    raw_path = normalize_path(raw_path)
    segments = [sanitize_folder_segment(s) for s in raw_path.split("/")]
    segments = [s for s in segments if s]
    if not segments:
        return None
    clean_path = "/".join(segments)
    with _db_lock:
        db = load_db()
        already_existed = clean_path in all_folder_paths_from(db)
        register_folder_path(db, clean_path)
        save_db(db)
    return clean_path, already_existed


def all_folder_paths_from(db):
    paths = set(db.get("folders", []))
    for r in db.get("files", []):
        fp = normalize_path(r.get("folder_path", ""))
        while fp:
            paths.add(fp)
            fp = "/".join(fp.split("/")[:-1])
    return paths


def all_folder_paths():
    return sorted(all_folder_paths_from(load_db()))


# ---------------------------------------------------------------------------
# EXIF metadata extraction
# ---------------------------------------------------------------------------

def extract_captured_date(filepath):
    if Image is None:
        return None
    try:
        with Image.open(filepath) as img:
            exif_data = img._getexif() if hasattr(img, "_getexif") else None
            if not exif_data:
                return None
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, tag_id)
                if tag_name == "DateTimeOriginal":
                    return value.replace(":", "-", 2)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Discord webhook helpers
# ---------------------------------------------------------------------------

# Discord's free-tier webhooks are rate-limited, and bursts of chunk uploads
# (especially several files at once, or one file split into many chunks)
# can trip that limit — the "429 ... rate limited" errors. Discord tells us
# exactly how long to wait via `retry_after`, so instead of failing the
# whole upload, we back off and retry automatically before giving up.
DISCORD_MAX_RETRIES = 6

def push_bytes_to_discord(data_bytes, filename, upload_id=None, max_retries=DISCORD_MAX_RETRIES):
    url = f"{DISCORD_WEBHOOK_URL}?wait=true"
    attempt = 0

    while True:
        # A fresh BytesIO each attempt — the previous one may have been
        # partially/fully consumed by the last (failed) request.
        files = {"file": (filename, io.BytesIO(data_bytes))}
        resp = requests.post(url, files=files, timeout=60)

        if resp.status_code in (200, 201):
            if upload_id:
                with _progress_lock:
                    if upload_id in UPLOAD_PROGRESS:
                        UPLOAD_PROGRESS[upload_id]["note"] = None
            payload = resp.json()
            message_id = payload.get("id")
            attachments = payload.get("attachments", [])
            cdn_url = attachments[0]["url"] if attachments else None
            return message_id, cdn_url

        retryable = resp.status_code == 429 or resp.status_code >= 500
        if retryable and attempt < max_retries:
            if resp.status_code == 429:
                try:
                    wait_seconds = float(resp.json().get("retry_after", 1.0)) + 0.25
                except Exception:
                    wait_seconds = 1.5
                note = f"Rate limited by Discord — retrying in {wait_seconds:.1f}s (attempt {attempt + 1}/{max_retries})"
            else:
                wait_seconds = min(2 ** attempt, 10)
                note = f"Discord had a hiccup ({resp.status_code}) — retrying in {wait_seconds:.1f}s (attempt {attempt + 1}/{max_retries})"

            if upload_id:
                with _progress_lock:
                    if upload_id in UPLOAD_PROGRESS:
                        UPLOAD_PROGRESS[upload_id]["note"] = note

            time.sleep(wait_seconds)
            attempt += 1
            continue

        raise RuntimeError(
            f"Discord upload failed ({resp.status_code}): {resp.text[:300]}"
        )


def delete_from_discord(message_id):
    url = f"{DISCORD_WEBHOOK_URL}/messages/{message_id}"
    resp = requests.delete(url, timeout=30)
    return resp.status_code in (200, 204, 404)


def reassemble_file_bytes(record):
    buf = io.BytesIO()
    for chunk in sorted(record["chunks"], key=lambda c: c["index"]):
        resp = requests.get(chunk["cdn_url"], timeout=60)
        resp.raise_for_status()
        buf.write(resp.content)
    return buf.getvalue()


def stream_reassembled_file(record):
    for chunk in sorted(record["chunks"], key=lambda c: c["index"]):
        resp = requests.get(chunk["cdn_url"], stream=True, timeout=60)
        for piece in resp.iter_content(chunk_size=8192):
            yield piece


# ---------------------------------------------------------------------------
# Background upload job — this is what makes progress "live"
# ---------------------------------------------------------------------------

def _process_upload_job(upload_id, temp_path, original_name, relative_path, folder_path, source):
    """Runs in a background thread. Splits the file into chunks, uploads
    each one to Discord, and keeps UPLOAD_PROGRESS[upload_id] current the
    whole way through so the frontend can poll live status + an ETA."""
    chunks_meta = []
    try:
        total_size = os.path.getsize(temp_path)
        total_chunks = max(1, math.ceil(total_size / CHUNK_SIZE))
        captured_date = extract_captured_date(temp_path)

        with open(temp_path, "rb") as f:
            index = 0
            while True:
                data = f.read(CHUNK_SIZE)
                if not data:
                    break
                index += 1
                chunk_name = (
                    original_name
                    if total_chunks == 1
                    else f"{original_name}.part{index:03d}of{total_chunks:03d}"
                )
                message_id, cdn_url = push_bytes_to_discord(data, chunk_name, upload_id=upload_id)
                chunks_meta.append({
                    "index": index,
                    "message_id": message_id,
                    "cdn_url": cdn_url,
                    "size_bytes": len(data),
                })
                with _progress_lock:
                    prog = UPLOAD_PROGRESS[upload_id]
                    prog["chunk_index"] = index
                    prog["bytes_uploaded"] = sum(c["size_bytes"] for c in chunks_meta)

        if not chunks_meta:  # empty-file edge case
            message_id, cdn_url = push_bytes_to_discord(b"", original_name, upload_id=upload_id)
            chunks_meta.append({
                "index": 1, "message_id": message_id, "cdn_url": cdn_url, "size_bytes": 0
            })

        record = {
            "id": uuid.uuid4().hex,
            "filename": original_name,
            "relative_path": relative_path,
            "folder_path": folder_path,
            "size_bytes": total_size,
            "is_chunked": len(chunks_meta) > 1,
            "chunk_count": len(chunks_meta),
            "chunks": chunks_meta,
            "captured_date": captured_date,
            "uploaded_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "source": source,  # which tab this was uploaded from: gallery / folders / all
        }
        add_file_record(record)

        with _progress_lock:
            UPLOAD_PROGRESS[upload_id].update({
                "status": "done",
                "record": record,
                "finished_at": time.time(),
            })

    except Exception as exc:
        for c in chunks_meta:
            try:
                delete_from_discord(c["message_id"])
            except Exception:
                pass
        with _progress_lock:
            UPLOAD_PROGRESS[upload_id].update({
                "status": "error",
                "error": str(exc),
                "finished_at": time.time(),
            })
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — API: files
# ---------------------------------------------------------------------------

@app.route("/api/files", methods=["GET"])
def list_files():
    records = load_db()["files"]
    records.sort(key=lambda r: r.get("uploaded_at", ""), reverse=True)
    return jsonify(records)


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """
    Accepts the file plus:
      - relative_path: e.g. "Vacation/Hawaii/beach.jpg" (folder placement)
      - source: which tab triggered the upload ("gallery" / "folders" / "all")

    Saves the file locally, then hands the actual Discord-chunking work off
    to a background thread and returns immediately with an `upload_id` the
    frontend polls at /api/upload/status/<upload_id> for live progress —
    this is what makes the upload bar track real, ongoing server-side work
    instead of freezing at 100% while Discord chunks are still going out.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    upload = request.files["file"]
    if upload.filename == "":
        return jsonify({"error": "No file selected"}), 400

    relative_path_raw = request.form.get("relative_path", upload.filename) or upload.filename
    relative_path = normalize_path(relative_path_raw)
    folder_path = os.path.dirname(relative_path)
    source = request.form.get("source", "unknown")

    original_name = os.path.basename(relative_path) or upload.filename or "unnamed_file"
    disk_safe_name = secure_filename(original_name) or uuid.uuid4().hex
    temp_name = f"{uuid.uuid4().hex}_{disk_safe_name}"
    temp_path = os.path.join(TEMP_STORAGE, temp_name)
    upload.save(temp_path)

    total_size = os.path.getsize(temp_path)
    total_chunks = max(1, math.ceil(total_size / CHUNK_SIZE))
    upload_id = uuid.uuid4().hex

    with _progress_lock:
        UPLOAD_PROGRESS[upload_id] = {
            "status": "in_progress",
            "chunk_index": 0,
            "chunk_total": total_chunks,
            "bytes_uploaded": 0,
            "total_bytes": total_size,
            "error": None,
            "note": None,
            "record": None,
        }

    _purge_old_progress()

    thread = threading.Thread(
        target=_process_upload_job,
        args=(upload_id, temp_path, original_name, relative_path, folder_path, source),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "upload_id": upload_id,
        "chunk_total": total_chunks,
        "total_bytes": total_size,
    }), 202


@app.route("/api/upload/status/<upload_id>", methods=["GET"])
def upload_status(upload_id):
    with _progress_lock:
        prog = UPLOAD_PROGRESS.get(upload_id)
    if not prog:
        return jsonify({"error": "Unknown upload_id"}), 404
    return jsonify(prog)


@app.route("/api/files/<file_id>", methods=["DELETE"])
def delete_file(file_id):
    record = find_file_record(file_id)
    if not record:
        return jsonify({"error": "File not found"}), 404

    all_ok = True
    for chunk in record.get("chunks", []):
        if not delete_from_discord(chunk["message_id"]):
            all_ok = False

    if not all_ok:
        return jsonify({"error": "Failed to delete one or more chunks from Discord"}), 502

    remove_file_record(file_id)
    return jsonify({"success": True})


@app.route("/api/files/bulk-delete", methods=["POST"])
def bulk_delete():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    deleted, failed = [], []

    for file_id in ids:
        record = find_file_record(file_id)
        if not record:
            failed.append(file_id)
            continue
        ok = all(delete_from_discord(c["message_id"]) for c in record.get("chunks", []))
        if ok:
            remove_file_record(file_id)
            deleted.append(file_id)
        else:
            failed.append(file_id)

    return jsonify({"deleted": deleted, "failed": failed})


@app.route("/api/files/<file_id>/download", methods=["GET"])
def download_file(file_id):
    """Reassemble a file's chunks (in order) and stream it back as one
    download with the original filename."""
    record = find_file_record(file_id)
    if not record or not record.get("chunks"):
        return jsonify({"error": "File not found"}), 404

    content_type = mimetypes.guess_type(record["filename"])[0] or "application/octet-stream"
    return Response(
        stream_reassembled_file(record),
        content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{record["filename"]}"'},
    )


@app.route("/api/files/<file_id>/preview", methods=["GET"])
def preview_file(file_id):
    """
    Stream a file INLINE (not as a download) for the lightbox preview,
    with HTTP Range support — this is what lets even a large video that
    got split into several Discord chunks still be watched/scrubbed
    before it's fully downloaded, since the <video> tag can request just
    the byte range it needs to keep playing.
    """
    record = find_file_record(file_id)
    if not record or not record.get("chunks"):
        return jsonify({"error": "File not found"}), 404

    chunks = sorted(record["chunks"], key=lambda c: c["index"])
    total_size = sum(c["size_bytes"] for c in chunks)
    content_type = mimetypes.guess_type(record["filename"])[0] or "application/octet-stream"

    range_header = request.headers.get("Range")
    if not range_header:
        return Response(
            stream_reassembled_file(record),
            content_type=content_type,
            headers={
                "Content-Disposition": f'inline; filename="{record["filename"]}"',
                "Accept-Ranges": "bytes",
            },
        )

    match = re.match(r"bytes=(\d*)-(\d*)", range_header)
    start = int(match.group(1)) if match and match.group(1) else 0
    end = int(match.group(2)) if match and match.group(2) else total_size - 1
    end = min(end, total_size - 1)
    length = max(0, end - start + 1)

    # Map absolute byte offsets to each chunk's [start, end] span so we
    # only fetch the chunk(s) that actually overlap the requested range.
    spans = []
    running = 0
    for c in chunks:
        spans.append((running, running + c["size_bytes"] - 1, c))
        running += c["size_bytes"]

    def generate_range():
        remaining = length
        pos = start
        for chunk_start, chunk_end, c in spans:
            if remaining <= 0:
                break
            if chunk_end < pos or chunk_start > start + length - 1:
                continue
            resp = requests.get(c["cdn_url"], timeout=60)
            data = resp.content
            local_start = max(0, pos - chunk_start)
            local_end = min(len(data), local_start + remaining)
            piece = data[local_start:local_end]
            if piece:
                yield piece
                remaining -= len(piece)
                pos += len(piece)

    rv = Response(generate_range(), status=206, content_type=content_type)
    rv.headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    rv.headers["Accept-Ranges"] = "bytes"
    rv.headers["Content-Length"] = str(length)
    return rv


@app.route("/api/files/bulk-download", methods=["POST"])
def bulk_download():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    records = [find_file_record(i) for i in ids]
    records = [r for r in records if r]

    if not records:
        return jsonify({"error": "No matching files found"}), 404

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for record in records:
            data = reassemble_file_bytes(record)
            arcname = record.get("relative_path") or record["filename"]
            base_arcname = arcname
            n = 1
            while arcname in used_names:
                root, ext = os.path.splitext(base_arcname)
                arcname = f"{root} ({n}){ext}"
                n += 1
            used_names.add(arcname)
            zf.writestr(arcname, data)

    mem_zip.seek(0)
    return send_file(
        mem_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name="nimbus_selected_files.zip",
    )


# ---------------------------------------------------------------------------
# Routes — API: folders
# ---------------------------------------------------------------------------

@app.route("/api/folders", methods=["GET"])
def get_folders():
    return jsonify(all_folder_paths())


@app.route("/api/folders", methods=["POST"])
def post_folder():
    data = request.get_json(silent=True) or {}
    raw_path = data.get("path", "")
    if not normalize_path(raw_path):
        return jsonify({"error": "Folder name is required"}), 400

    result = create_folder(raw_path)
    if not result:
        return jsonify({"error": "Invalid folder name"}), 400

    clean_path, already_existed = result
    if already_existed:
        return jsonify({"error": "A folder with that name already exists here", "path": clean_path}), 409

    return jsonify({"path": clean_path}), 201


@app.route("/api/folders/delete", methods=["POST"])
def delete_folder_route():
    """
    Delete a folder AND everything inside it — every file (its Discord
    messages get deleted too) and every nested subfolder, recursively.
    Path is sent as JSON (not a URL segment) since folder paths contain
    slashes.
    """
    data = request.get_json(silent=True) or {}
    raw_path = normalize_path(data.get("path", ""))
    if not raw_path:
        return jsonify({"error": "Folder path is required"}), 400

    prefix = raw_path + "/"
    db = load_db()
    target_files = [
        r for r in db["files"]
        if (r.get("folder_path") or "") == raw_path or (r.get("folder_path") or "").startswith(prefix)
    ]

    deleted_ids, failed_ids = [], []
    for r in target_files:
        ok = all(delete_from_discord(c["message_id"]) for c in r.get("chunks", []))
        if ok:
            deleted_ids.append(r["id"])
        else:
            failed_ids.append(r["id"])

    with _db_lock:
        db = load_db()
        db["files"] = [r for r in db["files"] if r["id"] not in deleted_ids]
        if not failed_ids:
            # Everything inside is gone — forget the folder (and any nested
            # subfolder paths) too, rather than leaving an empty husk behind.
            db["folders"] = [p for p in db["folders"] if p != raw_path and not p.startswith(prefix)]
        save_db(db)

    return jsonify({
        "path": raw_path,
        "deleted_files": deleted_ids,
        "failed_files": failed_ids,
        "folder_removed": not failed_ids,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
