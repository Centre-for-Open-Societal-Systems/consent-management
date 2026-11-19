"""The ECR names of the Consent Manager images: eoan/consent-management/<image>-<CM version>:<version>-<branch>.<build>.

    eoan/consent-management/consent-manager-api-1.0.2:0.0.0-develop.12

The CM version is appVersion of deployment/charts/openg2p-consent-manager/Chart.yaml (the upstream OpenG2P Consent
Manager the images track; commons-services pins that chart version). The tag starts with the chart's version. Same
standard as the registries (eoan/<registry>/<image>-<RP version>:<version>-<branch>.<build>).

Standard library only: it runs on the Jenkins agent's stock Python.
"""
import re
import sys
from pathlib import Path

CHART = Path(__file__).resolve().parents[1] / "deployment/charts/openg2p-consent-manager/Chart.yaml"
PATH = "eoan/consent-management"


def _chart_field(name):
    match = re.search(r"""(?m)^%s:\s*["']?([^"'\s]+)""" % name, CHART.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("%s has no %s" % (CHART.name, name))
    return match.group(1)


def repository(image):
    return "%s/%s-%s" % (PATH, image, _chart_field("appVersion"))


def tag(branch, build):
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    if not slug or not re.fullmatch(r"[1-9]\d*", str(build)):
        raise ValueError("an image tag needs a branch and a build number")
    return "%s-%s.%s" % (_chart_field("version"), slug, build)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "tag":
            print(tag(sys.argv[2], sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[1] == "repository":
            print(repository(sys.argv[2]))
        else:
            sys.exit("usage: image_names.py tag <branch> <build> | repository <image>")
    except (ValueError, OSError) as error:
        sys.exit("FAIL: %s" % error)
