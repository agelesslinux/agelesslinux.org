#!/usr/bin/env python3
"""generate-rss.py — RSS feed generator for agelesslinux.org

Two modes of operation:

1. Pre-commit hook (automatic): Detects new HTML pages in staged changes
   and adds entries to rss.xml. Only fires on brand-new files.

2. Manual invocation: Pass filenames and descriptions as arguments to add
   entries for substantive content updates to existing pages. This is
   intended to be called by Claude during the commit workflow when changes
   are newsworthy (not formatting, footer links, or date bumps).

   Usage: generate-rss.py update <file> <description> [<file> <description> ...]
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.etree import ElementTree as ET

SITE_URL = "https://agelesslinux.org"
FEED_FILE = "rss.xml"
FEED_TITLE = "Ageless Linux: Updates"
FEED_DESCRIPTION = "News and Commentary from the Ageless Linux project"

# Namespace for Atom self-link (required for valid RSS 2.0 + Atom)
ATOM_NS = "http://www.w3.org/2005/Atom"


def git(*args):
    """Run a git command and return (stdout, returncode)."""
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
    )
    return result.stdout.strip(), result.returncode


def get_diff_base():
    """Return the reference to diff against. Handles initial commit."""
    _, rc = git("rev-parse", "--verify", "HEAD")
    if rc != 0:
        # No HEAD yet (initial commit) — use the empty tree
        out, _ = git("hash-object", "-t", "tree", "/dev/null")
        return out
    return "HEAD"


def get_new_html_files(base):
    """HTML files being added for the first time in this commit."""
    out, _ = git("diff", "--cached", "--diff-filter=A", "--name-only", base, "--", "*.html")
    if not out:
        return []
    return [f for f in out.split("\n") if f.endswith(".html")]


def get_staged_content(filepath):
    """Read a file's content from the staging area (not the working tree)."""
    out, _ = git("show", f":{filepath}")
    return out


def extract_title(html):
    """Pull the <title> text out of an HTML document."""
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        # Unescape basic HTML entities for the RSS title
        title = m.group(1).strip()
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        return title
    return None


def extract_meta_description(html):
    """Pull <meta name="description" content="..."> if present."""
    m = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    return m.group(1).strip() if m else None


def create_feed():
    """Build a fresh RSS 2.0 XML tree."""
    ET.register_namespace("atom", ATOM_NS)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = SITE_URL
    ET.SubElement(channel, "description").text = FEED_DESCRIPTION
    ET.SubElement(channel, "language").text = "en-us"

    atom_link = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom_link.set("href", f"{SITE_URL}/{FEED_FILE}")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    return ET.ElementTree(rss)


def load_feed():
    """Load existing rss.xml or create a new feed."""
    ET.register_namespace("atom", ATOM_NS)
    if os.path.exists(FEED_FILE):
        return ET.parse(FEED_FILE)
    return create_feed()


def add_item(channel, title, link, description, pub_date, guid):
    """Insert a new <item> at the top of the feed (most recent first).

    If an item with the same GUID already exists, it is replaced. This
    prevents duplicates if the hook fires twice (e.g., aborted commit retry).
    """
    # Remove any existing item with the same GUID
    for existing in channel.findall("item"):
        existing_guid = existing.find("guid")
        if existing_guid is not None and existing_guid.text == guid:
            channel.remove(existing)

    item = ET.Element("item")
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "link").text = link
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text = format_datetime(pub_date)
    guid_el = ET.SubElement(item, "guid", isPermaLink="false")
    guid_el.text = guid

    # Insert before the first existing <item> to keep newest-first order
    existing_items = channel.findall("item")
    if existing_items:
        idx = list(channel).index(existing_items[0])
        channel.insert(idx, item)
    else:
        channel.append(item)


def write_feed(tree):
    """Write rss.xml and stage it for the current commit."""
    ET.indent(tree, space="  ")
    tree.write(FEED_FILE, encoding="unicode", xml_declaration=True)
    with open(FEED_FILE, "a") as f:
        f.write("\n")
    git("add", FEED_FILE)


def cmd_hook():
    """Pre-commit hook mode: add RSS entries for new HTML files only."""
    base = get_diff_base()
    new_files = get_new_html_files(base)

    if not new_files:
        return 0

    tree = load_feed()
    channel = tree.getroot().find("channel")
    now = datetime.now(timezone.utc)

    last_build = channel.find("lastBuildDate")
    if last_build is None:
        last_build = ET.SubElement(channel, "lastBuildDate")
    last_build.text = format_datetime(now)

    for filepath in new_files:
        html = get_staged_content(filepath)
        title = extract_title(html) or filepath
        url = f"{SITE_URL}/{filepath}"
        desc = extract_meta_description(html) or title
        guid = url

        add_item(channel, title, url, desc, now, guid)
        print(f"rss: new → {filepath}")

    write_feed(tree)
    return 0


def cmd_update(args):
    """Manual mode: add RSS entries for substantive updates to existing pages.

    Args should be pairs of (filepath, description).
    Usage: generate-rss.py update distros.html "Added Arch 32, updated MidnightBSD"
    """
    if len(args) < 2 or len(args) % 2 != 0:
        print("Usage: generate-rss.py update <file> <description> [<file> <description> ...]")
        return 1

    pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]

    tree = load_feed()
    channel = tree.getroot().find("channel")
    now = datetime.now(timezone.utc)

    last_build = channel.find("lastBuildDate")
    if last_build is None:
        last_build = ET.SubElement(channel, "lastBuildDate")
    last_build.text = format_datetime(now)

    for filepath, description in pairs:
        # Read from working tree (not staged) since this is called before staging
        if os.path.exists(filepath):
            with open(filepath) as f:
                html = f.read()
        else:
            print(f"rss: skipping {filepath} (file not found)")
            continue

        title = extract_title(html) or filepath
        url = f"{SITE_URL}/{filepath}"
        guid = f"{url}#updated-{now.strftime('%Y-%m-%d')}"

        add_item(channel, title, url, description, now, guid)
        print(f"rss: update → {filepath}")

    write_feed(tree)
    return 0


def main():
    root, _ = git("rev-parse", "--show-toplevel")
    os.chdir(root)

    if len(sys.argv) > 1 and sys.argv[1] == "update":
        return cmd_update(sys.argv[2:])
    return cmd_hook()


if __name__ == "__main__":
    sys.exit(main())
