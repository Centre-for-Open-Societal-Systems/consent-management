#!/usr/bin/env python3
"""Safety check for upgrading the shared commons-services release.

Compares the manifest that is live in the cluster (`helm get manifest`) with the
manifest an upgrade would apply (the MANIFEST section of `helm upgrade
--dry-run`). The build may only change image references. Exit 1 when a resource
is added or removed, or when any line other than an image reference changes, so
a Consent Manager build can never alter Keycloak, master-data, IAM, ODK or any
other part of commons.

usage: manifest_guard.py LIVE_MANIFEST NEW_MANIFEST_OR_DRYRUN_OUTPUT [REPORT]
"""
import difflib
import os
import re
import sys

# Optional: only resources whose "Kind/name" matches this regex may change at all
# (e.g. GUARD_ONLY='/commons-services-cm-'). Anything else that changes blocks.
ONLY = os.environ.get("GUARD_ONLY", "")

# Lines whose change is an image reference: `image: repo:tag@sha256:...`,
# `tag:` / `digest:` values, or any line carrying a sha256 digest.
ALLOWED = re.compile(r"^\s*(-\s*)?(image|imageTag|tag|digest):\s|@sha256:|^\s*digest:\s")


def manifest_part(text):
    """Return only the MANIFEST section when given `helm upgrade --dry-run` output."""
    if re.search(r"(?m)^MANIFEST:\s*$", text):
        text = re.split(r"(?m)^MANIFEST:\s*$", text, maxsplit=1)[1]
        text = re.split(r"(?m)^(HOOKS|NOTES):\s*$", text, maxsplit=1)[0]
    return text


def resources(text):
    out = {}
    for chunk in re.split(r"(?m)^---\s*$", manifest_part(text)):
        if not chunk.strip():
            continue
        kind = re.search(r"(?m)^kind:\s*(\S+)", chunk)
        if not kind:
            continue
        name = None
        in_meta = False
        for line in chunk.splitlines():
            if re.match(r"^metadata:\s*$", line):
                in_meta = True
                continue
            if in_meta:
                m = re.match(r"^  name:\s*[\"']?([^\s\"']+)", line)
                if m:
                    name = m.group(1)
                    break
                if re.match(r"^\S", line):
                    break
        out["%s/%s" % (kind.group(1), name or "?")] = chunk.strip("\n")
    return out


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    live = resources(open(argv[1]).read())
    new = resources(open(argv[2]).read())
    lines = []
    bad = False

    for key in sorted(set(live) - set(new)):
        lines.append("REMOVED  %s" % key)
        bad = True
    for key in sorted(set(new) - set(live)):
        lines.append("ADDED    %s" % key)
        bad = True

    changed = []
    for key in sorted(set(live) & set(new)):
        if live[key] == new[key]:
            continue
        diff = [d for d in difflib.unified_diff(live[key].splitlines(), new[key].splitlines(), lineterm="", n=0)
                if d[:1] in "+-" and not d.startswith(("+++", "---"))]
        other = [d for d in diff if not ALLOWED.search(d[1:])]
        changed.append(key)
        if ONLY and not re.search(ONLY, key):
            bad = True
            lines.append("CHANGED  %s  (outside GUARD_ONLY=%s)" % (key, ONLY))
        elif other:
            bad = True
            lines.append("CHANGED  %s  (non-image changes)" % key)
            lines.extend("    " + d[:200] for d in other[:12])
        else:
            lines.append("image    %s" % key)

    lines.append("")
    lines.append("resources live=%d new=%d changed=%d -> %s" % (len(live), len(new), len(changed), "BLOCKED" if bad else "ok (image changes only)"))
    report = "\n".join(lines)
    print(report)
    if len(argv) > 3:
        open(argv[3], "w").write(report + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
