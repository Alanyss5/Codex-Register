from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import paramiko


BLACKLIST_KEY = "email.domain_blacklist"
DEFAULT_DB_PATH = "/opt/codex-console/data/database.db"


@dataclass(frozen=True)
class ServerConfig:
    name: str
    host: str
    user: str
    password: str
    db_path: str = DEFAULT_DB_PATH


DEFAULT_SERVERS = [
    ServerConfig(name="srv24", host="23.140.140.70", user="root", password="33nKDS80L6ipmedJ3y0j"),
    ServerConfig(name="srv198", host="198.46.152.138", user="root", password="s3IEn8ClaxA4H99Zv7"),
]


def normalize_blacklist(items: Iterable[object]) -> list[str]:
    normalized = {
        str(item).strip().lower()
        for item in items
        if item is not None and str(item).strip()
    }
    return sorted(normalized)


def merge_blacklists(*blacklists: Iterable[object]) -> list[str]:
    merged: list[object] = []
    for blacklist in blacklists:
        merged.extend(list(blacklist))
    return normalize_blacklist(merged)


def _sha256_for_items(items: Iterable[str]) -> str:
    payload = json.dumps(list(items), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connect(server: ServerConfig) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(server.host, username=server.user, password=server.password, timeout=30, banner_timeout=30)
    return ssh


def _run_python(ssh: paramiko.SSHClient, script: str) -> str:
    command = f"python3 - <<'PY'\n{script}\nPY"
    stdin, stdout, stderr = ssh.exec_command(command, timeout=60)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if err:
        raise RuntimeError(err)
    return out


def fetch_blacklist(server: ServerConfig) -> list[str]:
    ssh = _connect(server)
    try:
        output = _run_python(
            ssh,
            f"""
import json, sqlite3
conn = sqlite3.connect({server.db_path!r})
cur = conn.cursor()
cur.execute("select value from settings where key=?", ({BLACKLIST_KEY!r},))
row = cur.fetchone()
print(row[0] if row and row[0] else "[]")
""",
        )
        return normalize_blacklist(json.loads(output or "[]"))
    finally:
        ssh.close()


def write_blacklist(server: ServerConfig, blacklist: list[str]) -> dict[str, object]:
    ssh = _connect(server)
    try:
        payload = json.dumps(blacklist, ensure_ascii=False)
        output = _run_python(
            ssh,
            f"""
import json, sqlite3
payload = json.dumps(json.loads({payload!r}), ensure_ascii=False)
conn = sqlite3.connect({server.db_path!r})
cur = conn.cursor()
cur.execute("select 1 from settings where key=?", ({BLACKLIST_KEY!r},))
exists = cur.fetchone() is not None
if exists:
    cur.execute(
        "update settings set value=?, description=?, category='email' where key=?",
        (payload, "被 OpenAI 拒绝注册的邮箱域名黑名单", {BLACKLIST_KEY!r}),
    )
else:
    cur.execute(
        "insert into settings(key, value, description, category) values(?, ?, ?, 'email')",
        ({BLACKLIST_KEY!r}, payload, "被 OpenAI 拒绝注册的邮箱域名黑名单"),
    )
conn.commit()
cur.execute("select value from settings where key=?", ({BLACKLIST_KEY!r},))
row = cur.fetchone()
data = sorted(json.loads(row[0])) if row and row[0] else []
print(json.dumps({{"count": len(data), "sha256": __import__("hashlib").sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()}}, ensure_ascii=False))
""",
        )
        return json.loads(output)
    finally:
        ssh.close()


def backup_blacklists(backup_dir: Path, server_blacklists: dict[str, list[str]]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for server_name, blacklist in server_blacklists.items():
        path = backup_dir / f"{server_name}-{BLACKLIST_KEY}-{timestamp}.json"
        path.write_text(json.dumps(blacklist, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_blacklists(*, backup_dir: Path, dry_run: bool = False) -> dict[str, object]:
    current = {server.name: fetch_blacklist(server) for server in DEFAULT_SERVERS}
    backup_blacklists(backup_dir, current)
    merged = merge_blacklists(*current.values())

    summary: dict[str, object] = {
        "servers": {name: len(items) for name, items in current.items()},
        "intersection_count": len(set(current["srv24"]) & set(current["srv198"])),
        "union_count": len(merged),
        "merged_sha256": _sha256_for_items(merged),
        "dry_run": dry_run,
    }

    merged_path = backup_dir / "merged-email.domain_blacklist.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    if dry_run:
        return summary

    writes = {server.name: write_blacklist(server, merged) for server in DEFAULT_SERVERS}
    summary["writes"] = writes
    summary["consistent"] = len({json.dumps(item, sort_keys=True) for item in writes.values()}) == 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge and sync email.domain_blacklist between srv24 and srv198.")
    parser.add_argument("--dry-run", action="store_true", help="Only fetch, merge, and print summary without writing back.")
    parser.add_argument(
        "--backup-dir",
        default="temp_blacklist_backups",
        help="Local directory for fetched backups and merged output.",
    )
    args = parser.parse_args()

    summary = sync_blacklists(
        backup_dir=Path(args.backup_dir),
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
