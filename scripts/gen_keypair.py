"""Create an RSA key pair for the DataMind Snowflake service user (key-pair auth, no password).

Writes the private key OUTSIDE the repository (default ~/.datamind/snowflake_rsa_key.p8), then prints the
ALTER USER statement to run as ACCOUNTADMIN and the line to add to .env. The private key is never printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path.home() / ".datamind" / "snowflake_rsa_key.p8")
    parser.add_argument("--user", default="DATAMIND_APP")
    parser.add_argument("--slot", choices=["1", "2"], default="1", help="use 2 for a second laptop on the same user")
    parser.add_argument("--force", action="store_true", help="replace an existing key file")
    args = parser.parse_args()

    out = args.out.expanduser().resolve()
    if out == REPO or REPO in out.parents:
        print("Refusing to write a private key inside the repository.", file=sys.stderr)
        return 1
    if out.exists() and not args.force:
        print(f"{out} already exists; pass --force to replace it.", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    out.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    try:
        out.chmod(0o600)
    except OSError:
        pass
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    out.with_suffix(".pub").write_text(public_pem, encoding="utf-8")
    body = "".join(line for line in public_pem.splitlines() if "-----" not in line)
    slot = "" if args.slot == "1" else "_2"
    print(f"Private key written to {out} (keep it private; never commit it).\n")
    print("1) Run in Snowflake as ACCOUNTADMIN:")
    print(f"ALTER USER {args.user} SET RSA_PUBLIC_KEY{slot} = '{body}';\n")
    print("2) Add to your .env:")
    print(f"SNOWFLAKE_USER={args.user}")
    print(f"SNOWFLAKE_PRIVATE_KEY_PATH={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
