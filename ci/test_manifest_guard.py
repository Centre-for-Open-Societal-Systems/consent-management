"""The upgrade guard of ci/deploy-dev.sh: only Consent Manager image references may change in commons-services.

Standard library only (runs on the Jenkins agent's stock Python).
    python3 -m unittest discover -s ci -p 'test_*.py' -v
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manifest_guard", ROOT / "ci/manifest_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def deployment(name, image, checksum="aaa"):
    return """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: %s
spec:
  template:
    metadata:
      annotations:
        oan.org/values-checksum: '%s'
    spec:
      containers:
        - name: app
          image: %s
""" % (name, checksum, image)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.only, guard.ONLY = guard.ONLY, "/commons-services-cm-"
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        guard.ONLY = self.only
        self.dir.cleanup()

    def run_guard(self, live, new):
        paths = []
        for name, text in (("live.yaml", live), ("new.yaml", new)):
            paths.append(os.path.join(self.dir.name, name))
            Path(paths[-1]).write_text(text, encoding="utf-8")
        return guard.main(["manifest_guard.py"] + paths)

    def test_values_checksum_change_outside_consent_is_allowed(self):
        live = deployment("commons-services-esignet", "esignet:1", "aaa") + deployment("commons-services-cm-api", "cm:1")
        new = deployment("commons-services-esignet", "esignet:1", "bbb") + deployment("commons-services-cm-api", "cm:2")
        self.assertEqual(self.run_guard(live, new), 0)

    def test_any_other_change_outside_consent_still_blocks(self):
        live = deployment("commons-services-esignet", "esignet:1", "aaa")
        new = deployment("commons-services-esignet", "esignet:2", "bbb")
        self.assertEqual(self.run_guard(live, new), 1)

    def test_jobs_named_after_the_release_revision_are_the_same_job(self):
        self.revision, guard.REVISION = guard.REVISION, "48"
        self.addCleanup(setattr, guard, "REVISION", self.revision)
        job = "---\nkind: Job\nmetadata:\n  name: commons-services-cm-kc-init-%s-c9435ed8\n  labels:\n    job: commons-services-kc-sa-role-%s\n"
        self.assertEqual(self.run_guard(job % (48, 48), job % (49, 49)), 0)


if __name__ == "__main__":
    unittest.main()
