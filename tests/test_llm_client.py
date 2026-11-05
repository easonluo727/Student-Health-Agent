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


def client_answering(answers, fallbacks="", backups=""):
    """A client whose requests get answers[model] (a list, consumed in order); returns (client, models asked).
    Models on the backup provider are asked as "backup:<model>"."""
    asked = []

    def handler(request):
        model = json.loads(request.content)["model"]
        if request.url.host == "backup.test":
            model = "backup:" + model
        asked.append(model)
        if isinstance(answers[model], Exception):
            raise answers[model]
        status, body = answers[model].pop(0) if len(answers[model]) > 1 else answers[model][0]
        return httpx2.Response(status, json=body)

    with mock.patch.dict(os.environ, {**ENV, "LLM_FALLBACK_MODELS": fallbacks, "LLM_BACKUP_MODELS": backups,
                                      "LLM_BACKUP_BASE_URL": "https://backup.test/v1", "LLM_BACKUP_API_KEY": "k"}):
        client = llm_client.make_client(max_retries=3)
    client._client = httpx2.Client(transport=httpx2.MockTransport(handler))
    if client.backup_client:
        client.backup_client._client = httpx2.Client(transport=httpx2.MockTransport(handler))
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

    def test_a_404_with_only_the_message_also_falls_back(self):
        plain = (404, {"error": {"message": "The requested model was not found.", "type": "invalid_request_error"}})
        client, asked = client_answering({"main": [plain], "b": [OK]}, "b")
        self.assertEqual(post(client), {"ok": True})
        self.assertEqual(asked, ["main", "b"])
        bare = (404, {"message": "The requested model was not found."})
        client, asked = client_answering({"main": [bare], "b": [OK]}, "b")
        self.assertEqual(post(client), {"ok": True})

    def test_other_404_is_not_retried(self):
        client, asked = client_answering({"main": [(404, {"error": {"code": "not_found", "message": "No route."}})]}, "b")
        with self.assertRaises(openai.NotFoundError):
            post(client)
        self.assertEqual(asked, ["main"])

    def test_gateway_error_falls_back_after_the_sdk_retries(self):
        down = (503, {"error": {"message": "Internet Connection Server Error"}})
        client, asked = client_answering({"main": [down], "b": [OK]}, "b")
        self.assertEqual(post(client), {"ok": True})
        self.assertEqual(asked, ["main"] * 4 + ["b"])  # the SDK's 3 retries, then the next model

    def test_gateway_error_is_not_tried_again_on_the_same_model(self):
        down = (503, {"error": {"message": "Internet Connection Server Error"}})
        client, asked = client_answering({"main": [down], "b": [down]}, "b")
        with self.assertRaises(openai.InternalServerError):
            post(client)
        self.assertEqual(asked, ["main"] * 4 + ["b"] * 4)

    def test_a_bad_request_is_not_retried_on_another_model(self):
        client, asked = client_answering({"main": [(400, {"error": {"message": "Bad input."}})]}, "b")
        with self.assertRaises(openai.BadRequestError):
            post(client)
        self.assertEqual(asked, ["main"])


    def test_backup_provider_after_every_model_is_down(self):
        down = (503, {"error": {"message": "Internet Connection Server Error"}})
        client, asked = client_answering({"main": [down], "b": [NOT_FOUND], "backup:x": [down], "backup:y": [OK]},
                                         "b", "x, y")
        self.assertEqual(post(client), {"ok": True})
        self.assertEqual(asked, ["main"] * 4 + ["b", "b"] + ["backup:x"] * 4 + ["backup:y"])

    def test_no_connection_moves_to_the_backup(self):
        client, asked = client_answering({"main": httpx2.ConnectError("no route"), "backup:x": [OK]}, "", "x")
        self.assertEqual(post(client), {"ok": True})
        self.assertEqual(asked, ["main"] * 4 + ["backup:x"])

    def test_without_backup_models_there_is_no_backup(self):
        client, _ = client_answering({"main": [OK]})
        self.assertIsNone(client.backup_client)


if __name__ == "__main__":
    unittest.main()
