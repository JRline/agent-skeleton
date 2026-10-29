"""Settings from the environment: typed and checked once at start (agent/config.py Settings)."""
import unittest
from pathlib import Path
from unittest import mock

from agent import config


class EnvSettings(unittest.TestCase):
    def test_defaults_without_environment(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            s = config.load_settings()
        self.assertEqual((s.backend, s.max_steps, s.sql_timeout_s, s.model_memory), ("fake", 15, 900, "off"))

    def test_environment_variables_override_by_their_documented_names(self):
        env = {"LLM_BACKEND": "claude", "AGENT_MAX_STEPS": "25", "SQL_TIMEOUT_S": "60", "MODEL_MEMORY": "session",
               "AGENT_SKILLS": "/tmp/skills", "BASIC_TOOLKITS": "files, analysis"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = config.load_settings()
        self.assertEqual((s.backend, s.max_steps, s.sql_timeout_s, s.model_memory), ("claude", 25, 60, "session"))
        self.assertEqual(s.skills_dir, Path("/tmp/skills"))
        self.assertEqual(s.basic_toolkits, "files, analysis")

    def test_a_bad_value_names_the_variable(self):
        for env, name in [({"SQL_TIMEOUT_S": "abc"}, "SQL_TIMEOUT_S"), ({"MODEL_MEMORY": "yes"}, "MODEL_MEMORY"),
                          ({"AGENT_MAX_STEPS": "0"}, "AGENT_MAX_STEPS"), ({"LLM_BACKEND": "gpt"}, "LLM_BACKEND")]:
            with mock.patch.dict("os.environ", env, clear=True), self.assertRaisesRegex(RuntimeError, name):
                config.load_settings()

    def test_backends_list_matches_the_allowed_values(self):
        self.assertEqual(config.BACKENDS, ["fake", "ollama", "claude", "chatgpt", "langchain", "prod"])
