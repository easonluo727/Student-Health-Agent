"""The LLM client retries Meta's spurious 404 model_not_found, and nothing else that is a 404 (no network)."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

import llm_client  # noqa: E402

ENV = {"LLM_BASE_URL": "https://llm.test/v1", "LLM_API_KEY": "test-key", "LLM_MODEL": "test-model"}
OK = {"id": "m", "object": "list", "data": []}


def client_answering(*responses):
    """A client whose requests get the given (status, json) answers in order; returns (client, call count)."""
    calls = []

    def handler(request):
        status, body = responses[min(len(calls), len(responses) - 1)]
        calls.append(request)
        return httpx2.Response(status, json=body)

    with mock.patch.dict(os.environ, ENV):
        client = llm_client.make_client(max_retries=3)
    client._client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return client, calls


class RetryTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("time.sleep")  # skip the SDK's backoff
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_model_not_found_is_retried(self):
        not_found = (404, {"error": {"code": "model_not_found", "message": "The requested model was not found."}})
        client, calls = client_answering(not_found, (200, OK))
        client.models.list()
        self.assertEqual(len(calls), 2)

    def test_other_404_is_not_retried(self):
        import openai

        client, calls = client_answering((404, {"error": {"code": "not_found", "message": "No such route."}}))
        with self.assertRaises(openai.NotFoundError):
            client.models.list()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
