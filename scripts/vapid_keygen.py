"""Print a fresh VAPID key pair in `.env` form. Run via `just vapid-keygen`.

Generate the pair once per deployment and keep it: the public key is
baked into every browser subscription, so a rotated pair means every
device must enable notifications again.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kodji.services.webpush import generate_vapid_keys  # noqa: E402


def main() -> int:
    private, public = generate_vapid_keys()
    print("# Add to .env (both required). Keep the private key secret.")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
