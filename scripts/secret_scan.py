"""Pre-commit secret scan. Prints file:line and the rule name, never the matched text.

Scans every tracked or untracked-but-not-ignored file (git-ignored files such as .env
are never opened) and checks that .env is ignored and untracked.
Exit code 0 = clean, 1 = findings.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000

RULES_ALL = {
    "private-key-block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "google-api-key": re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    "snowflake-url-with-password": re.compile(r"snowflake://[^:\s/]+:[^@\s]+@", re.I),
}
# Quoted literal assigned to a secret-looking name (code files).
RULE_CODE = re.compile(
    r"(?i)\w*(password|passwd|secret|api_?key|token|passphrase)\w*\s*[:=]\s*['\"][A-Za-z0-9_\-+/=.]{16,}['\"]"
)
# KEY=value lines in config-like files.
RULE_CONFIG = re.compile(r"(?im)^\s*\w*(PASSWORD|SECRET|API_KEY|TOKEN|PASSPHRASE)\w*\s*[=:]\s*[^\s#'\"]{8,}")
CONFIG_SUFFIXES = {".env", ".toml", ".ini", ".cfg", ".yaml", ".yml", ".conf", ".properties"}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


def candidate_files() -> list[Path]:
    out = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [ROOT / p for p in out.split("\0") if p]


def scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:4096]:
        return []  # binary
    text = data.decode("utf-8", errors="replace")
    rules = dict(RULES_ALL)
    if path.suffix in {".py", ".js", ".ts", ".json"}:
        rules["quoted-secret-literal"] = RULE_CODE
    if path.suffix in CONFIG_SUFFIXES or path.name.startswith(".env"):
        rules["secret-config-value"] = RULE_CONFIG
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, rx in rules.items():
            if rx.search(line):
                hits.append((lineno, name))
    return hits


def main() -> int:
    problems: list[str] = []
    if git("ls-files", ".env").strip():
        problems.append(".env: tracked by git (must never be committed)")
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False).returncode == 0
    if not ignored:
        problems.append(".gitignore: .env is not ignored")
    files = candidate_files()
    for path in files:
        if path.resolve() == Path(__file__).resolve():
            continue
        for lineno, rule in scan_file(path):
            problems.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {rule}")
    if problems:
        print(f"SECRET SCAN FAILED ({len(problems)} finding(s)):")
        for p in problems[:20]:
            print("  " + p)
        return 1
    print(f"secret scan: clean ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
