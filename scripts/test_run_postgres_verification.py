"""Runner regressions for Docker credential arguments.

Imports the verification script and checks the command it actually builds.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_postgres_verification as runner


PASSWORD = "synthetic-runner-password-7c2e9a"
INHERITED_AWS = "inherited-aws-secret-not-approved"
INHERITED_DATABASE = "postgresql://inherited.example/not-used"
INHERITED_PG = "inherited-pg-password-not-approved"


class DockerCredentialArgumentTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "PHOENIX_VANZ_DOCKER_IMAGE",
                "PHOENIX_VANZ_VENV",
                "AWS_SECRET_ACCESS_KEY",
                "DATABASE_URL",
                "PGPASSWORD",
            )
        }
        os.environ["PHOENIX_VANZ_DOCKER_IMAGE"] = "phoenix-vanz-django52-py311:local"
        os.environ["PHOENIX_VANZ_VENV"] = "/opt/venv"
        os.environ["AWS_SECRET_ACCESS_KEY"] = INHERITED_AWS
        os.environ["DATABASE_URL"] = INHERITED_DATABASE
        os.environ["PGPASSWORD"] = INHERITED_PG

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _child_env(self):
        return {
            "DJANGO_SETTINGS_MODULE": "base.settings_postgres_tests",
            "PHOENIX_VANZ_POSTGRES_TESTS": "1",
            "PHOENIX_VANZ_POSTGRES_HOST": "127.0.0.1",
            "PHOENIX_VANZ_POSTGRES_PORT": "5432",
            "PHOENIX_VANZ_POSTGRES_NAME": "phoenix_vanz_pgtest",
            "PHOENIX_VANZ_POSTGRES_TEST_NAME": "phoenix_vanz_pgtest_django",
            "PHOENIX_VANZ_POSTGRES_USER": "phoenix_vanz_app",
            "PHOENIX_VANZ_POSTGRES_PASSWORD": PASSWORD,
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
        }

    def test_docker_arguments_name_the_password_without_its_value(self):
        child_env = self._child_env()
        process_env = runner._controlled_process_env(child_env)
        command, label = runner._django_command(
            ["manage.py", "test", "--verbosity", "1"],
            child_env,
            process_env,
        )
        rendered = "\n".join(command)
        self.assertNotIn(PASSWORD, rendered)
        self.assertNotIn(PASSWORD, label)
        self.assertNotIn(INHERITED_AWS, rendered)
        self.assertNotIn(INHERITED_DATABASE, rendered)
        self.assertNotIn(INHERITED_PG, rendered)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", rendered)
        self.assertNotIn("DATABASE_URL", command)
        password_at = command.index("PHOENIX_VANZ_POSTGRES_PASSWORD")
        self.assertEqual(command[password_at - 1], "-e")
        self.assertNotIn("=", command[password_at])
        self.assertEqual(process_env["PHOENIX_VANZ_POSTGRES_PASSWORD"], PASSWORD)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", process_env)
        self.assertNotIn("DATABASE_URL", process_env)
        self.assertNotIn("PGPASSWORD", process_env)

    def test_postgres_container_arguments_omit_the_password(self):
        command, process_env = runner._postgres_container_launch(55999, PASSWORD)
        rendered = "\n".join(command)
        self.assertNotIn(PASSWORD, rendered)
        self.assertNotIn(INHERITED_AWS, rendered)
        self.assertNotIn(INHERITED_DATABASE, rendered)
        self.assertNotIn(INHERITED_PG, rendered)
        password_at = command.index("POSTGRES_PASSWORD")
        self.assertEqual(command[password_at - 1], "--env")
        self.assertNotIn("=", command[password_at])
        self.assertEqual(process_env["POSTGRES_PASSWORD"], PASSWORD)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", process_env)
        self.assertNotIn("DATABASE_URL", process_env)

    def test_failed_launch_diagnostic_omits_the_password_and_stays_nonzero(self):
        _command, process_env = runner._postgres_container_launch(55999, PASSWORD)
        completed = runner._run(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stderr.write(os.environ.get('POSTGRES_PASSWORD', '')); sys.exit(7)",
            ],
            capture_output=True,
            env=process_env,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.returncode, 7)
        diagnostic = runner._diagnostic(completed, [PASSWORD])
        self.assertNotIn(PASSWORD, diagnostic)
        self.assertIn("exit=7", diagnostic)
        self.assertIsInstance(completed, subprocess.CompletedProcess)


if __name__ == "__main__":
    unittest.main()
