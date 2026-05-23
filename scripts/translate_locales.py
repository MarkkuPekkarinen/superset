#!/usr/bin/env python3
"""Fill Superset PO catalogs (sv, et) via Google translate endpoint.

Usage:
  REQUESTS_CA_BUNDLE=/etc/pki/tls/cert.pem SSL_CERT_FILE=/etc/pki/tls/cert.pem \
  python3 scripts/translate_locales.py --langs sv et
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polib

PLACEHOLDER_PATTERNS = [
    r"%\([^)]+\)s",
    r"%[sdif]",
    r"\{[^{}]+\}",
    r"\$\{[^{}]+\}",
]
PLACEHOLDER_RE = re.compile("|".join(f"({p})" for p in PLACEHOLDER_PATTERNS))


def protect_placeholders(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    idx = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal idx
        token = f"__PH_{idx}__"
        mapping[token] = match.group(0)
        idx += 1
        return token

    return PLACEHOLDER_RE.sub(repl, text), mapping


def restore_placeholders(text: str, mapping: dict[str, str]) -> str:
    out = text
    for token, original in mapping.items():
        out = out.replace(token, original)
    return out


def translate_text(
    text: str,
    lang: str,
    cache: dict[tuple[str, str], str],
    ssl_context: ssl.SSLContext,
) -> str:
    if not text.strip():
        return text

    key = (text, lang)
    if key in cache:
        return cache[key]

    safe, mapping = protect_placeholders(text)
    url = (
        "https://translate.googleapis.com/translate_a/single?"
        + urllib.parse.urlencode(
            {
                "client": "gtx",
                "sl": "en",
                "tl": lang,
                "dt": "t",
                "q": safe,
            }
        )
    )

    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=15, context=ssl_context) as response:
                payload = json.loads(response.read().decode("utf-8"))
            translated = "".join(
                part[0] for part in payload[0] if part and part[0] is not None
            )
            translated = restore_placeholders(translated, mapping)
            cache[key] = translated
            return translated
        except Exception:
            if attempt == 5:
                # Fallback: keep source text if translation repeatedly fails.
                cache[key] = text
                return text
            time.sleep(0.3 * (attempt + 1))

    cache[key] = text
    return text


def process_lang(
    lang: str,
    ssl_context: ssl.SSLContext,
    progress_every: int,
    max_changes: int | None,
    workers: int,
) -> tuple[int, int, int]:
    po_path = Path(f"superset/translations/{lang}/LC_MESSAGES/messages.po")
    if not po_path.exists():
        raise FileNotFoundError(f"Missing PO file: {po_path}")

    po = polib.pofile(str(po_path), check_for_duplicates=False)
    cache: dict[tuple[str, str], str] = {}

    # Build target list first to avoid scanning/mutating in the same loop
    targets: list[tuple[polib.POEntry, str | None, str]] = []
    total = 0
    for entry in po:
        if entry.obsolete:
            continue
        if entry.msgid_plural:
            for plural_key, current in list(entry.msgstr_plural.items()):
                source = entry.msgid if plural_key == "0" else entry.msgid_plural
                cur = (current or "").strip()
                if source.strip() and ((not cur) or cur == source.strip()):
                    targets.append((entry, plural_key, source))
                total += 1
        else:
            source = (entry.msgid or "").strip()
            current = (entry.msgstr or "").strip()
            if source and ((not current) or current == source):
                targets.append((entry, None, source))
            total += 1

    if max_changes is not None:
        targets = targets[:max_changes]

    changed = 0

    # Translate in parallel; apply sequentially to keep PO object mutation safe
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(translate_text, src, lang, cache, ssl_context): (entry, pkey, src)
            for entry, pkey, src in targets
        }

        for idx, future in enumerate(as_completed(future_map), start=1):
            entry, plural_key, _src = future_map[future]
            translated = future.result()
            if plural_key is None:
                entry.msgstr = translated
            else:
                entry.msgstr_plural[plural_key] = translated
            changed += 1

            if progress_every and changed % progress_every == 0:
                po.metadata["Language"] = lang
                po.metadata["Language-Team"] = f"{lang} <LL@li.org>"
                po.save(str(po_path))
                print(f"{lang}: progress translated={changed}/{len(targets)}", flush=True)

    po.metadata["Language"] = lang
    po.metadata["Language-Team"] = f"{lang} <LL@li.org>"
    po.save(str(po_path))

    # completeness snapshot
    empty = 0
    for entry in po:
        if entry.obsolete:
            continue
        if entry.msgid_plural:
            for val in entry.msgstr_plural.values():
                if not (val or "").strip():
                    empty += 1
        else:
            if not (entry.msgstr or "").strip():
                empty += 1

    return total, changed, empty


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate Superset locale catalogs")
    parser.add_argument(
        "--langs",
        nargs="+",
        default=["sv", "et"],
        help="Locale codes to process (default: sv et)",
    )
    parser.add_argument(
        "--cafile",
        default="/etc/pki/tls/cert.pem",
        help="CA bundle path for HTTPS verification",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print and checkpoint progress every N processed entries (default: 100)",
    )
    parser.add_argument(
        "--max-changes",
        type=int,
        default=None,
        help="Optional cap for changed entries per run (useful for chunked execution)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel translation workers per language (default: 8)",
    )
    args = parser.parse_args()

    ssl_context = ssl.create_default_context(cafile=args.cafile)

    for lang in args.langs:
        total, changed, empty = process_lang(
            lang,
            ssl_context,
            progress_every=args.progress_every,
            max_changes=args.max_changes,
            workers=args.workers,
        )
        if empty >= 0:
            print(f"{lang}: total={total} changed={changed} empty={empty}")
        else:
            print(f"{lang}: total={total} changed={changed} empty=not-computed (early stop)")


if __name__ == "__main__":
    main()
