"""On Meta's 404 model_not_found the client moves to the next model in LLM_FALLBACK_MODELS (no network)."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx2
import openai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

import llm_client  # noqa: E402

ENV = {"LLM_BASE_URL": "https://llm.test/v1", "LLM_API_KEY": "test-key", "LLM_MODEL": "main"}
NOT_FOUND = (404, {"error": {"code": "model_not_found", "message": "The requested model was not found."}})
OK = (200, {"ok": True})


def client_answering(answers, fallbacks=""):
    """A client whose requests get answers[model] (a list, consumed in order); returns (client, models asked)."""
    asked = []

    def handler(request):
        model = json.loads(request.content)["model"]
        asked.append(model)
        status, body = answers[model].pop(0) if len(answers[model]) > 1 else answers[model][0]
        return httpx2.Response(status, json=body)

    with mock.patch.dict(os.environ, {**ENV, "LLM_FALLBACK_MODELS": fallbacks}):
        client = llm_client.make_client(max_retries=3)
    client._client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return client, asked


def post(client, model="main"):
    return client.post("/responses", body={"model": model, "input": "hi"}, cast_to=object)


class FallbackTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("time.sleep")  # skip the SDK's backoff
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unavailable_model_falls_back_to_the_next(self):
        client, asked = client_answering({"main": [NOT_FOUND], "b": [NOT_FOUND], "c": [OK]}, "b, c")
        self.assertEqual(post(client), {"ok": True})
        self.assertEqual(asked, ["main", "b", "c"])

    def test_without_fallbacks_the_same_model_is_tried_twice(self):
        client, asked = client_answering({"main": [NOT_FOUND, OK]})
        self.assertEqual(post(client), {"ok": True})
        self.assertEqual(asked, ["main", "main"])

    def test_gives_up_after_two_rounds(self):
        client, asked = client_answering({"main": [NOT_FOUND], "b": [NOT_FOUND]}, "b")
        with self.assertRaises(openai.NotFoundError):
            post(client)
        self.assertEqual(asked, ["main", "b", "main", "b"])

    def test_other_404_is_not_retried(self):
        client, asked = client_answering({"main": [(404, {"error": {"code": "not_found", "message": "No route."}})]}, "b")
        with self.assertRaises(openai.NotFoundError):
            post(client)
        self.assertEqual(asked, ["main"])


if __name__ == "__main__":
    unittest.main()
