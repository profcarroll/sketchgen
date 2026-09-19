"""Unit tests for sketchgen.models — what this node has, and what it can do.

Run:  python3 -m unittest discover -s tests -v

No model host: every test serves Ollama's two endpoints from a dict, so the
capability rules are tested against the shapes a real Ollama produces —
including the one where ``/api/tags`` and ``/api/show`` disagree, which is why
this module exists.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import models  # noqa: E402

HOST = "http://127.0.0.1:11434"

#: The node's real answer on 2026-09-19, trimmed. gemma4:e4b is the important
#: row: its tags entry omits the vision and audio it actually has.
TAGS = {
    "models": [
        {"name": "gemma4:e4b", "size": 9608350718,
         "capabilities": ["completion", "tools", "thinking"],
         "details": {"parameter_size": "8.0B", "family": "gemma4"}},
        {"name": "qwen3.5:9b", "size": 6594474711,
         "capabilities": ["vision", "completion", "tools", "thinking"],
         "details": {"parameter_size": "9.7B", "family": "qwen35"}},
        {"name": "qwen3-coder:30b-a3b-q4_K_M", "size": 18556700761,
         "capabilities": ["completion", "tools"],
         "details": {"parameter_size": "30.5B", "family": "qwen3moe"}},
        {"name": "gemma4:31b-cloud", "size": 312, "remote_model": "gemma4:31b",
         "remote_host": "https://ollama.com",
         "capabilities": ["completion", "thinking", "tools", "vision"],
         "details": {"parameter_size": "32.7B"}},
        {"name": "nomic-embed-text:latest", "size": 274302450,
         "capabilities": ["embedding"], "details": {"parameter_size": "137M"}},
    ]
}

SHOW = {
    "gemma4:e4b": ["completion", "vision", "audio", "tools", "thinking"],
    "qwen3.5:9b": ["completion", "thinking", "tools", "vision"],
    "qwen3-coder:30b-a3b-q4_K_M": ["completion", "tools"],
    "gemma4:31b-cloud": ["completion", "thinking", "tools", "vision"],
    "nomic-embed-text:latest": ["embedding"],
}


def fake_ollama(tags=TAGS, show=SHOW, *, show_fails=()):
    """A stand-in for :func:`models._get`. Records the calls it was given."""
    calls = []

    def get(url, body=None, timeout=models.TIMEOUT_S):
        calls.append(url)
        if url.endswith("/api/tags"):
            return tags
        if url.endswith("/api/show"):
            name = (body or {}).get("model")
            if name in show_fails:
                return None
            return {"capabilities": list(show.get(name, []))}
        return None

    get.calls = calls
    return get


class TestCatalogue(unittest.TestCase):
    def setUp(self):
        models.forget()
        self.addCleanup(models.forget)

    def read(self, **kwargs):
        with mock.patch.object(models, "_get", fake_ollama(**kwargs)):
            return models.catalogue(HOST, refresh=True)

    def test_capabilities_come_from_show_not_from_tags(self):
        """The whole reason for the second call: tags lies about gemma4:e4b."""
        found = {model.name: model for model in self.read()}
        self.assertIn("vision", found["gemma4:e4b"].capabilities)
        self.assertIn("audio", found["gemma4:e4b"].capabilities)
        # ...and the tags list, which is what a one-call implementation would
        # have believed, says it has neither.
        claimed = next(m for m in TAGS["models"] if m["name"] == "gemma4:e4b")
        self.assertNotIn("vision", claimed["capabilities"])

    def test_a_model_show_will_not_answer_for_keeps_what_tags_claimed(self):
        found = {model.name: model for model in
                 self.read(show_fails=("qwen3.5:9b",))}
        # short, but not invented: the model is still offered
        self.assertEqual(
            {"vision", "completion", "tools", "thinking"},
            set(found["qwen3.5:9b"].capabilities),
        )

    def test_details_and_size_are_carried(self):
        found = {model.name: model for model in self.read()}
        self.assertEqual("9.7B", found["qwen3.5:9b"].parameter_size)
        self.assertEqual(6594474711, found["qwen3.5:9b"].size_bytes)
        self.assertEqual("qwen35", found["qwen3.5:9b"].family)

    def test_a_proxied_model_is_marked_remote_and_sorts_last(self):
        found = self.read()
        cloud = next(model for model in found if model.name == "gemma4:31b-cloud")
        self.assertTrue(cloud.remote)
        self.assertFalse(found[0].remote)
        self.assertIs(found[-1], cloud)

    def test_a_host_that_does_not_answer_is_an_empty_catalogue(self):
        with mock.patch.object(models, "_get", lambda *a, **k: None):
            self.assertEqual([], models.catalogue(HOST, refresh=True))

    def test_junk_instead_of_a_model_list_is_an_empty_catalogue(self):
        for body in ({"models": "not a list"}, {}, {"models": []},
                     {"models": [{"no": "name"}]}):
            with self.subTest(body=body):
                with mock.patch.object(models, "_get",
                                       lambda *a, _b=body, **k: _b):
                    self.assertEqual([], models.catalogue(HOST, refresh=True))

    def test_a_model_host_is_not_asked_twice_inside_the_ttl(self):
        get = fake_ollama()
        with mock.patch.object(models, "_get", get):
            models.catalogue(HOST, refresh=True, now=1000.0)
            first = len(get.calls)
            models.catalogue(HOST, now=1000.0 + models.CACHE_TTL_S / 2)
            self.assertEqual(first, len(get.calls))
            models.catalogue(HOST, now=1000.0 + models.CACHE_TTL_S + 1)
            self.assertGreater(len(get.calls), first)

    def test_an_empty_answer_is_cached_too(self):
        """A host that is down must not be waited on once per request."""
        calls = []

        def down(url, body=None, timeout=models.TIMEOUT_S):
            calls.append(url)
            return None

        with mock.patch.object(models, "_get", down):
            models.catalogue(HOST, refresh=True, now=1000.0)
            self.assertEqual([], models.catalogue(HOST, now=1000.5))
            self.assertEqual(1, len(calls))

    def test_forget_drops_one_host_or_all_of_them(self):
        get = fake_ollama()
        with mock.patch.object(models, "_get", get):
            models.catalogue(HOST, refresh=True, now=1000.0)
            models.forget(HOST)
            models.catalogue(HOST, now=1000.0)
        self.assertEqual(2, get.calls.count(HOST + "/api/tags"))


class TestQuestions(unittest.TestCase):
    def setUp(self):
        models.forget()
        self.addCleanup(models.forget)
        with mock.patch.object(models, "_get", fake_ollama()):
            self.found = models.catalogue(HOST, refresh=True)

    def test_vision_is_the_planner_filter_and_the_coder_fails_it(self):
        names = [m.name for m in
                 models.with_capability(self.found, models.PLANNER_CAPABILITY)]
        self.assertIn("gemma4:e4b", names)
        self.assertIn("qwen3.5:9b", names)
        self.assertNotIn("qwen3-coder:30b-a3b-q4_K_M", names)
        self.assertNotIn("nomic-embed-text:latest", names)

    def test_remote_models_can_be_left_out(self):
        names = [m.name for m in models.with_capability(
            self.found, "vision", include_remote=False)]
        self.assertNotIn("gemma4:31b-cloud", names)

    def test_an_embedding_model_can_never_execute(self):
        names = [m.name for m in
                 models.with_capability(self.found, models.EXECUTOR_CAPABILITY)]
        self.assertIn("qwen3-coder:30b-a3b-q4_K_M", names)
        self.assertNotIn("nomic-embed-text:latest", names)

    def test_a_label_says_the_tag_the_size_and_what_it_can_do(self):
        gemma = next(m for m in self.found if m.name == "gemma4:e4b")
        label = models.label(gemma, default="gemma4:e4b")
        self.assertIn("gemma4:e4b", label)
        self.assertIn("8.0B", label)
        self.assertIn("8.9 GB", label)
        self.assertIn("vision", label)
        self.assertIn("(default)", label)
        # tools and thinking are on every model in this build: naming them on
        # every row costs the width the tag and the size need
        self.assertNotIn("tools", label)
        self.assertNotIn("thinking", label)

    def test_a_caller_can_narrow_what_the_label_mentions(self):
        """A menu already filtered to vision should not print it on every row."""
        gemma = next(m for m in self.found if m.name == "gemma4:e4b")
        label = models.label(gemma, mention=("audio",))
        self.assertIn("audio", label)
        self.assertNotIn("vision", label)

    def test_a_proxied_models_label_does_not_claim_a_size(self):
        """Its 312 bytes of manifest would render as 0.0 GB, which reads free."""
        cloud = next(m for m in self.found if m.name == "gemma4:31b-cloud")
        label = models.label(cloud)
        self.assertIn("32.7B", label)
        self.assertNotIn("GB", label)

    def test_a_label_only_says_default_for_the_default(self):
        other = next(m for m in self.found if m.name == "qwen3.5:9b")
        self.assertNotIn("(default)", models.label(other, default="gemma4:e4b"))


if __name__ == "__main__":
    unittest.main()
