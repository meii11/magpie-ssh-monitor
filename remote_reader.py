"""Runs through SSH using Python's standard library; never writes remote files."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

UTC = dt.timezone.utc
MAX_LINE = 128 * 1024 * 1024


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("日志缺少时间戳")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("日志时间戳缺少时区")
    return parsed.astimezone(UTC)


def anchor(stream, offset):
    stream.seek(max(0, offset - 256))
    return hashlib.blake2s(stream.read(min(offset, 256)), digest_size=16).hexdigest()


def read_file(path, cursor, since, byte_budget):
    """Return metadata only. Advance a cursor exclusively over complete lines."""
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    offset = cursor.get("offset", 0)
    with path.open("rb") as stream:
        replay = (cursor.get("identity") != identity or offset > stat.st_size)
        if not replay and offset:
            replay = anchor(stream, offset) != cursor.get("anchor")
            if offset == stat.st_size and cursor.get("mtime_ns") != stat.st_mtime_ns:
                replay = True
        state = {} if replay else dict(cursor)
        offset = 0 if replay else offset
        stream.seek(offset)
        events, consumed = [], 0
        while consumed < byte_budget:
            start = stream.tell()
            line = stream.readline(MAX_LINE + 1)
            if len(line) > MAX_LINE:
                raise ValueError(f"单条日志超过 128 MiB：{path.name}")
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            consumed += len(line)
            event = json.loads(line)
            kind, body = event.get("type"), event.get("payload")
            if not isinstance(body, dict):
                continue
            if kind == "session_meta":
                session = body.get("id")
                if not isinstance(session, str) or not session:
                    raise ValueError("session_meta 缺少会话 ID")
                if state.get("session") and state["session"] != session:
                    raise ValueError("同一日志文件的会话 ID 发生变化")
                state["session"] = session
                if body.get("model_provider"):
                    state["provider"] = body["model_provider"]
            elif kind == "turn_context":
                if body.get("turn_id") and body.get("model"):
                    events.append({"kind": "context", "session": state.get("session"),
                                   "turn": body["turn_id"], "model": body["model"],
                                   "effort": body.get("effort") or ""})
            elif kind == "event_msg" and body.get("type") == "token_count":
                if isinstance(body.get("rate_limits"), dict):
                    source = body["rate_limits"]
                    limits = {key: source[key] for key in ("limit_id", "plan_type") if key in source}
                    for key in ("primary", "secondary"):
                        window = source.get(key)
                        limits[key] = ({k: window[k] for k in ("used_percent", "window_minutes", "resets_at") if k in window}
                                       if isinstance(window, dict) else None)
                    events.append({"kind": "quota", "t": event.get("timestamp"),
                                   "limits": limits, "session": state.get("session")})
            elif kind in ("token_usage_record", "compacted"):
                record = body if kind == "token_usage_record" else body.get("latest_token_usage_record")
                if record is None:
                    continue
                at = event.get("timestamp")
                if timestamp(at) >= since:
                    safe = {k: record[k] for k in ("response_id", "session_id", "thread_id", "turn_id") if k in record}
                    if isinstance(record.get("usage"), dict):
                        safe["usage"] = {k: record["usage"][k] for k in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens") if k in record["usage"]}
                    events.append({"kind": "usage", "t": at, "record": safe,
                                   "provider": state.get("provider"), "file_session": state.get("session")})
        offset = stream.tell()
        digest = anchor(stream, offset)
    state.update(offset=offset, identity=identity, anchor=digest, mtime_ns=stat.st_mtime_ns)
    return events, state, consumed, offset < stat.st_size


def scan(request, root=None):
    root = Path(root) if root is not None else Path(request["remote_dir"]).expanduser()
    if not root.is_dir():
        raise ValueError("服务器日志目录不存在：" + str(root))
    since = timestamp(request["since"])
    cursors, updates, events = request.get("cursors", {}), {}, []
    budget = request.get("byte_budget", 256 * 1024 * 1024)
    consumed, more, scanned = 0, False, 0
    candidates = []
    for parent, _, names in os.walk(root):
        for name in names:
            if not name.endswith((".jsonl", ".jsonl.zst")):
                continue
            path = Path(parent) / name
            stat = path.stat()
            relative = str(path.relative_to(root))
            if relative not in cursors and stat.st_mtime < since.timestamp():
                continue
            if name.endswith(".zst"):
                raise ValueError("范围内存在压缩日志，首版不支持：" + relative)
            candidates.append((relative, path))
    for relative, path in sorted(candidates):
        cursor = cursors.get(relative, {})
        stat = path.stat()
        if (cursor.get("offset") == stat.st_size and cursor.get("mtime_ns") == stat.st_mtime_ns
                and cursor.get("identity") == [stat.st_dev, stat.st_ino]):
            continue
        if consumed >= budget:
            more = True
            break
        rows, update, size, pending = read_file(path, cursor, since, budget - consumed)
        updates[relative] = update
        events.extend(rows)
        consumed += size
        scanned += 1
        # An unfinished final line waits for the next normal poll, not a busy loop.
        if pending and size >= budget - (consumed - size):
            more = True
    return {"version": 1, "events": events, "cursors": updates, "more": more,
            "bytes_read": consumed, "files_read": scanned, "files_known": len(candidates),
            "collected_at": dt.datetime.now(UTC).isoformat()}


if __name__ == "__main__":
    json.dump(scan(json.load(sys.stdin)), sys.stdout, separators=(",", ":"))
