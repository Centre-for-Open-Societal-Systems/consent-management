"""Write the commons-services values that run this build's Consent Manager images, each pinned by digest.

    python3 ci/image_values.py <live-values.json> <build-dir> <image-tag> > images.json

live-values.json is `helm get values <release> -a -o json` of the live release. build-dir holds
repository-<image> and digest-<image> from ci/build-push.sh. The output is JSON, which helm reads as a
values file.

The API image is set in two places: consentManagerApi and every extraApis entry that pins its own image
(the partner api does, in the commons-services defaults). extraApis is a list, and helm replaces a list
whole, so the live list is given back with only those images changed.

Standard library only: it runs on the Jenkins agent's stock Python.
"""
import copy
import json
from pathlib import Path
import sys

CM = "openg2p-consent-manager"


def _image(build_dir, name):
    read = lambda kind: (Path(build_dir) / ("%s-%s" % (kind, name))).read_text(encoding="utf-8").strip()
    repository, digest = read("repository"), read("digest")
    if not repository or not digest.startswith("sha256:"):
        raise ValueError("no pushed image recorded for %s" % name)
    return repository, digest


def image_values(live, build_dir, tag):
    def pinned(name):
        repository, digest = _image(build_dir, name)
        return {"repository": repository, "tag": "%s@%s" % (tag, digest)}

    api = pinned("consent-manager-api")
    extra_apis = copy.deepcopy(live.get(CM, {}).get("extraApis") or [])
    for entry in extra_apis:
        if "image" in entry:
            entry["image"] = dict(entry["image"], **api)

    portal_repository, portal_digest = _image(build_dir, "consent-manager-partner-portal")
    return {
        CM: {
            "consentManagerApi": {"image": api},
            "extraApis": extra_apis,
            "ui": {"image": pinned("consent-manager-ui")},
            "sanity": {"image": pinned("consent-manager-sanity")},
        },
        # commons-services' own template: the digest wins over any tag.
        "consentPartnerPortal": {"image": {"repository": portal_repository, "digest": portal_digest}},
    }


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: image_values.py <live-values.json> <build-dir> <image-tag>")
    try:
        live = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        print(json.dumps(image_values(live, sys.argv[2], sys.argv[3]), indent=2))
    except (ValueError, OSError) as error:
        sys.exit("FAIL: %s" % error)
