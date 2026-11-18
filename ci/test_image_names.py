"""Image names: eoan/consent-management/<image>-<CM version>:<version>-<branch>.<build>, both versions from Chart.yaml.

Standard library only (runs on the Jenkins agent's stock Python).
    python3 -m unittest discover -s ci -p 'test_*.py' -v
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_names", ROOT / "ci/image_names.py")
image_names = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_names)


class ImageNameTests(unittest.TestCase):
    def test_repository_carries_the_consent_manager_version_of_the_chart(self):
        self.assertEqual(image_names.repository("consent-manager-api"),
                         "eoan/consent-management/consent-manager-api-1.0.2")

    def test_tag_is_the_chart_version_branch_and_build_number(self):
        self.assertEqual(image_names.tag("develop", 12), "0.0.0-develop.12")

    def test_tag_turns_any_branch_into_a_docker_tag(self):
        self.assertEqual(image_names.tag("ci/eoan-image_names", "7"), "0.0.0-ci-eoan-image-names.7")

    def test_tag_refuses_a_missing_branch_or_build_number(self):
        for branch, build in (("develop", "0"), ("develop", ""), ("develop", "3a"), ("//", "5")):
            with self.assertRaises(ValueError):
                image_names.tag(branch, build)

    def test_command_line(self):
        for argv, expected in ((["tag", "develop", "12"], "0.0.0-develop.12"),
                               (["repository", "consent-manager-ui"], "eoan/consent-management/consent-manager-ui-1.0.2")):
            done = subprocess.run([sys.executable, str(ROOT / "ci/image_names.py")] + argv, stdout=subprocess.PIPE,
                                  universal_newlines=True, check=True)
            self.assertEqual(done.stdout, expected + "\n")


if __name__ == "__main__":
    unittest.main()
