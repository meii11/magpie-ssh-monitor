"""Persistent, idempotent SSH-to-Magpie usage import. No third-party dependencies."""
import argparse
import datetime as dt
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.request

from remote_reader import timestamp

ROOT = Path(__file__).resolve().parent
COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        os.chmod(temporary, 0o600)
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def connect(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(state / "state.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY, body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS contexts(session TEXT, turn TEXT, model TEXT NOT NULL, effort TEXT,
        PRIMARY KEY(session,turn));
      CREATE TABLE IF NOT EXISTS records(rid TEXT PRIMARY KEY, session TEXT NOT NULL, turn TEXT NOT NULL,
        source_session TEXT NOT NULL, provider TEXT NOT NULL, t TEXT NOT NULL, counters TEXT NOT NULL,
        posted INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS quotas(id TEXT PRIMARY KEY, t TEXT NOT NULL, body TEXT NOT NULL);
    """)
    return db


def counts(value):
    if not isinstance(value, dict):
        raise ValueError("token_usage_record 缺少 usage")
    result = {}
    for key in COUNTERS:
        number = value.get(key, 0) if key not in ("input_tokens", "output_tokens") else value.get(key)
        if type(number) is not int or not 0 <= number <= 100_000_000:
            raise ValueError("无效的用量计数：" + key)
        result[key] = number
    if result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"]:
        raise ValueError("缓存 Token 超过总输入 Token")
    return result


def apply_scan(db, host, scan):
    if scan.get("version") != 1:
        raise ValueError("未知采集协议版本")
    with db:
        owner = db.execute("SELECT value FROM metadata WHERE key='host'").fetchone()
        if owner and owner[0] != host:
            raise ValueError("状态目录属于另一台服务器，不能混用采集进度")
        if owner is None:
            if db.execute("SELECT 1 FROM records WHERE rid NOT LIKE ? LIMIT 1", (host + ":%",)).fetchone():
                raise ValueError("已有请求记录属于另一台服务器")
            db.execute("INSERT INTO metadata VALUES('host',?)", (host,))
        for event in scan["events"]:
            kind = event["kind"]
            if kind == "context":
                key = (event["session"], event["turn"])
                if not all(isinstance(x, str) and x for x in key) or not event["model"]:
                    raise ValueError("模型上下文缺少明确会话和轮次")
                previous = db.execute("SELECT model FROM contexts WHERE session=? AND turn=?", key).fetchone()
                if previous and previous[0] != event["model"]:
                    raise ValueError("同一轮次出现不同模型，停止导入以免错误归属")
                db.execute("INSERT OR IGNORE INTO contexts VALUES(?,?,?,?)", (*key, event["model"], event["effort"]))
            elif kind == "quota":
                at, limits = event["t"], event["limits"]
                timestamp(at)
                identity = limits.get("limit_id") or "codex"
                previous = db.execute("SELECT t FROM quotas WHERE id=?", (identity,)).fetchone()
                if previous is None or timestamp(at) > timestamp(previous[0]):
                    db.execute("INSERT OR REPLACE INTO quotas VALUES(?,?,?)", (identity, at, json.dumps(event)))
            elif kind == "usage":
                record = event["record"]
                identity = record.get("response_id")
                source_session = record.get("session_id")
                session, turn, provider = record.get("thread_id"), record.get("turn_id"), event.get("provider")
                if not all(isinstance(x, str) and x for x in (identity, source_session, session, turn, provider)):
                    raise ValueError("请求记录缺少明确 ID、会话、模型服务商或轮次")
                timestamp(event["t"])
                usage = json.dumps(counts(record.get("usage")), sort_keys=True)
                rid = f"{host}:{source_session}:{identity}"
                if len(rid) > 256:
                    raise ValueError("请求 ID 超出 Magpie 长度限制")
                previous = db.execute("SELECT session,turn,source_session,provider,counters FROM records WHERE rid=?", (rid,)).fetchone()
                row = (session, turn, source_session, provider, usage)
                if previous and previous != row:
                    raise ValueError("同一请求的用量或归属发生修订，停止导入：" + identity)
                db.execute("INSERT OR IGNORE INTO records(rid,session,turn,source_session,provider,t,counters) VALUES(?,?,?,?,?,?,?)",
                           (rid, session, turn, source_session, provider, event["t"], usage))
            else:
                raise ValueError("未知采集事件：" + kind)
        for path, cursor in scan["cursors"].items():
            db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (path, json.dumps(cursor)))


def pending(db, host, agent=None):
    rows = db.execute("""SELECT r.rid,r.session,r.provider,r.t,r.counters,c.model,c.effort
        FROM records r JOIN contexts c ON c.session=r.session AND c.turn=r.turn
        WHERE r.posted=0 ORDER BY r.t,r.rid LIMIT 1000""").fetchall()
    result = []
    for rid, session, provider, at, raw, model, effort in rows:
        usage = json.loads(raw)
        # Codex counts cached input inside input_tokens; Magpie stores it separately.
        result.append({"rid": rid, "session": f"{host}/{session}", "agent": agent if agent is not None else f"SSH-{host}",
                       "provider": provider, "model": model, "t": at, "status": 200,
                       "in": usage["input_tokens"] - usage["cached_input_tokens"] - usage["cache_write_input_tokens"],
                       "out": usage["output_tokens"], "cache_read": usage["cached_input_tokens"],
                       "cache_write": usage["cache_write_input_tokens"],
                       "reasoning": usage["reasoning_output_tokens"], "effort": effort})
    return result


def post_records(db, gateway, records):
    request = urllib.request.Request(gateway + "/v1/magpie/usage/requests", data=json.dumps(records).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    # This local accounting request must not go through shell HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        receipt = json.load(response)
    accepted = receipt.get("records")
    if not isinstance(accepted, list) or len(accepted) != len(records):
        raise ValueError("Magpie 返回了不完整的导入回执")
    expected = {row["rid"] for row in records}
    actual = {row.get("rid") for row in accepted if row.get("status") in ("accepted", "skipped")}
    if actual != expected:
        raise ValueError("Magpie 导入回执与请求不一致")
    with db:
        db.executemany("UPDATE records SET posted=1 WHERE rid=?", [(rid,) for rid in expected])
    return receipt["accepted"], receipt["skipped"]


def sync(state, drain=False, no_import=False):
    config = json.loads((state / "config.json").read_text())
    host, gateway = config["host"], config["gateway"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise ValueError("host 必须是 SSH 配置中的明确别名")
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", gateway):
        raise ValueError("只允许向本机 Magpie 导入用量")
    with (state / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = connect(state)
        try:
            rounds, written, skipped, read_bytes = 0, 0, 0, 0
            while True:
                request = {"since": config["since"], "remote_dir": config["remote_dir"],
                           "cursors": {p: json.loads(c) for p, c in db.execute("SELECT path,body FROM cursors")}}
                code = (ROOT / "remote_reader.py").read_text()
                command = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", host,
                           "python3 -c " + shlex.quote(code)]
                process = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True, timeout=150)
                if process.returncode:
                    raise RuntimeError("SSH 采集失败：" + process.stderr.strip()[-1200:])
                scan = json.loads(process.stdout)
                apply_scan(db, host, scan)
                read_bytes += scan["bytes_read"]
                if not no_import:
                    while batch := pending(db, host, config["agent"]):
                        added, duplicate = post_records(db, gateway, batch)
                        written += added
                        skipped += duplicate
                total, posted = db.execute("SELECT COUNT(*),COALESCE(SUM(posted),0) FROM records").fetchone()
                unresolved = db.execute("""SELECT COUNT(*) FROM records r WHERE NOT EXISTS
                    (SELECT 1 FROM contexts c WHERE c.session=r.session AND c.turn=r.turn)""").fetchone()[0]
                value = {"version": 1, "host": host, "since": config["since"], "collected_at": scan["collected_at"],
                         "error": f"{unresolved} 条请求缺少明确模型上下文，尚未导入" if unresolved else "",
                         "catching_up": scan["more"], "records": total, "posted": posted,
                         "unresolved_models": unresolved,
                         "sessions": db.execute("SELECT COUNT(DISTINCT session) FROM records").fetchone()[0],
                         "quotas": [json.loads(row[0]) for row in db.execute("SELECT body FROM quotas ORDER BY id")]}
                atomic_json(state / "snapshot.json", value)
                rounds += 1
                if not drain or not scan["more"]:
                    return {"accepted": written, "skipped": skipped, "rounds": rounds,
                            "bytes_read": read_bytes, **{k: value[k] for k in ("records", "posted", "sessions", "unresolved_models", "catching_up")}}
                if rounds >= 64:
                    raise RuntimeError("初始采集超过 64 批次，停止并等待检查")
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sync", "status"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--no-import", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "status":
        print((args.state / "snapshot.json").read_text())
        return
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(args.state / "collector.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        result = sync(args.state, args.drain, args.no_import)
    except BlockingIOError:
        return  # Another scheduled/manual run owns the collector lock.
    except Exception as error:
        previous = args.state / "snapshot.json"
        value = json.loads(previous.read_text()) if previous.exists() else {"version": 1}
        config_file = args.state / "config.json"
        if "host" not in value and config_file.exists():
            value["host"] = json.loads(config_file.read_text())["host"]
        value["error"] = str(error)
        atomic_json(previous, value)
        logger.exception("采集失败")
        if not args.quiet:
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
    logger.info(json.dumps(result, ensure_ascii=False))
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
