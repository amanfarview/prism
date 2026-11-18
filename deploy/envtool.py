"""Set KEY=VALUE lines in a .env file without touching the others.

    python3 deploy/envtool.py .env PRISM_AUTH_MODE=entra PRISM_PUBLIC_URL=https://...
"""
import os
import sys


def main(path: str, pairs: list[str]) -> None:
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    for pair in pairs:
        key, _, value = pair.partition("=")
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
