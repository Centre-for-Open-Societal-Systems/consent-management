"""The image values ci/deploy-dev.sh gives commons-services: this build's images, pinned by digest.

Standard library only (runs on the Jenkins agent's stock Python).
    python3 -m unittest discover -s ci -p 'test_*.py' -v
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_values", ROOT / "ci/image_values.py")
image_values = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_values)

ECR = "379220350808.dkr.ecr.ap-south-1.amazonaws.com"
OLD_API = {"repository": ECR + "/openg2p/commons/consent-manager-api", "tag": "dev-48e64c249056@sha256:" + "c" * 64}
IMAGES = ("consent-manager-api", "consent-manager-ui", "consent-manager-sanity", "consent-manager-partner-portal")


def live_values():
    return {"openg2p-consent-manager": {
        "consentManagerApi": {"image": dict(OLD_API)},
        "extraApis": [{"enabled": True, "nameOverride": "partner-api", "apiAudience": "partner", "image": dict(OLD_API)},
                      {"enabled": False, "nameOverride": "bene-api"}]}}


class ImageValuesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for n, name in enumerate(IMAGES):
            Path(self.dir.name, "repository-" + name).write_text("%s/eoan/consent-management/%s-1.0.2\n" % (ECR, name))
            Path(self.dir.name, "digest-" + name).write_text("sha256:%s\n" % (str(n) * 64))

    def test_partner_api_in_extra_apis_runs_the_new_api_image(self):
        values = image_values.image_values(live_values(), self.dir.name, "0.0.0-develop.12")
        partner, bene = values["openg2p-consent-manager"]["extraApis"]
        self.assertEqual(partner["image"], {"repository": ECR + "/eoan/consent-management/consent-manager-api-1.0.2",
                                            "tag": "0.0.0-develop.12@sha256:" + "0" * 64})
        self.assertEqual(partner["nameOverride"], "partner-api")
        self.assertEqual(bene, {"enabled": False, "nameOverride": "bene-api"})

    def test_an_image_this_build_did_not_push_stops_the_deploy(self):
        Path(self.dir.name, "digest-consent-manager-ui").write_text("\n")
        with self.assertRaises(ValueError):
            image_values.image_values(live_values(), self.dir.name, "0.0.0-develop.12")


if __name__ == "__main__":
    unittest.main()
