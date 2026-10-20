"""A local chat endpoint supplies strict typed System1 answers without a key."""

from __future__ import annotations

import json
from typing import Any, Self

import pytest

import nott_system1
from nott_system1 import System1Unavailable, ask


class Response:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return json.dumps(
            {
                "choices": [
                    {"message": {"content": json.dumps({"answers": self.answer})}}
                ]
            }
        ).encode()


class EnvelopeResponse(Response):
    def read(self, size: int = -1) -> bytes:
        return json.dumps(self.answer).encode()


def test_local_request_constrains_all_answer_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    questions = {
        'risk."id': {"type": "noul", "instructions": "Check the risk."},
        "route": {
            "type": "choice",
            "criteria": {"accept": "Accept the input.", "reject": "Reject the input."},
        },
        "rank": {"type": "score", "instructions": "Score the input."},
    }
    answers = {
        'risk."id': {"type": "noul", "noul": 0.25},
        "route": {
            "type": "choice",
            "choice": "accept",
            "probabilities": {"accept": 0.75, "reject": 0.25},
            "confidence": 0.9,
        },
        "rank": {"type": "score", "score": -3.5, "confidence": 0.8},
    }
    seen = []

    def respond(request: Any, *, timeout: float) -> Response:
        seen.append(json.loads(request.data))
        assert timeout == 15
        assert request.get_header("Authorization") is None
        return Response(answers)

    monkeypatch.setattr(nott_system1, "_local_open", respond)
    result = ask(
        "private-state-marker",
        questions,
        endpoint="http://127.0.0.1:11434/v1/chat/completions",
        model="local-test",
        timeout=15,
        protocol="openai-compatible",
    )
    assert result['risk."id'].value == 0.25
    assert result["route"].probabilities == {"accept": 0.75, "reject": 0.25}
    assert result["rank"].value == -3.5
    body = seen[0]
    assert set(body) == {"model", "messages", "temperature", "response_format"}
    assert body["temperature"] == 0
    assert json.loads(body["messages"][1]["content"]) == {
        "state": "private-state-marker",
        "questions": questions,
    }
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "system1_answers"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == ["answers"]
    answer_map = schema["properties"]["answers"]
    assert set(answer_map["required"]) == set(questions)
    fields = answer_map["properties"]
    probability = {"type": "number", "minimum": 0, "maximum": 1}
    assert fields['risk."id']["properties"] == {
        "type": {"type": "string", "enum": ["noul"]},
        "noul": probability,
    }
    choice = fields["route"]["properties"]
    assert choice["type"] == {"type": "string", "enum": ["choice"]}
    assert choice["choice"] == {"type": "string", "enum": ["accept", "reject"]}
    assert choice["confidence"] == probability
    distribution = choice["probabilities"]
    assert distribution["properties"] == {"accept": probability, "reject": probability}
    assert fields["rank"]["properties"] == {
        "type": {"type": "string", "enum": ["score"]},
        "score": {"type": "number"},
        "confidence": probability,
    }
    for closed in (schema, answer_map, *fields.values(), distribution):
        assert closed["type"] == "object"
        assert closed["additionalProperties"] is False
        assert set(closed["required"]) == set(closed["properties"])


@pytest.mark.parametrize(
    ("envelope", "field"),
    [
        ({"private-response-marker": "secret"}, "choices"),
        ([], "choices"),
        ({"choices": []}, "choices"),
        ({"choices": {}}, "choices"),
        ({"choices": [{}]}, "message"),
        ({"choices": [{"message": {}}]}, "content"),
        ({"choices": [{"message": {"content": None}}]}, "content"),
        ({"choices": [{"message": {"content": "{}"}}]}, "answers"),
    ],
)
def test_local_envelope_error_names_only_the_protocol_field(
    monkeypatch: pytest.MonkeyPatch, envelope: object, field: str
) -> None:
    monkeypatch.setattr(
        nott_system1, "_local_open", lambda *a, **k: EnvelopeResponse(envelope)
    )
    with pytest.raises(System1Unavailable) as failure:
        ask(
            "private-state-marker",
            {"private-question-marker": {"type": "noul"}},
            endpoint="http://localhost:8080/v1/chat/completions",
            model="local-test",
            timeout=15,
            protocol="openai-compatible",
        )
    assert (
        str(failure.value)
        == f"no usable answer: missing or invalid protocol field: {field}"
    )


@pytest.mark.parametrize(
    ("question", "answer", "field"),
    [
        ({"type": "noul"}, {}, "type"),
        ({"type": "noul"}, {"type": "noul"}, "noul"),
        (
            {"type": "choice", "criteria": {"yes": "Accept."}},
            {"type": "choice"},
            "choice",
        ),
        (
            {"type": "choice", "criteria": {"yes": "Accept."}},
            {"type": "choice", "choice": "yes"},
            "probabilities",
        ),
        (
            {"type": "choice", "criteria": {"yes": "Accept."}},
            {"type": "choice", "choice": "yes", "probabilities": {"yes": 1}},
            "confidence",
        ),
        ({"type": "score"}, {"type": "score"}, "score"),
        ({"type": "score"}, {"type": "score", "score": 1}, "confidence"),
    ],
)
def test_local_missing_answer_field_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
    question: dict[str, Any],
    answer: dict[str, Any],
    field: str,
) -> None:
    monkeypatch.setattr(
        nott_system1, "_local_open", lambda *a, **k: Response({"private-id": answer})
    )
    with pytest.raises(System1Unavailable) as failure:
        ask(
            "private-state-marker",
            {"private-id": question},
            endpoint="http://localhost:8080/v1/chat/completions",
            model="local-test",
            timeout=15,
            protocol="openai-compatible",
        )
    assert (
        str(failure.value)
        == f"no usable answer: missing or invalid protocol field: {field}"
    )


@pytest.mark.parametrize(
    "question",
    [
        {},
        {"type": "private-unsupported-type"},
        {"type": "choice"},
        {"type": "choice", "criteria": {}},
        {"type": "choice", "criteria": ["yes"]},
        {"type": "choice", "criteria": {1: "Accept."}},
    ],
)
def test_local_invalid_question_cannot_reach_the_transport(
    monkeypatch: pytest.MonkeyPatch, question: dict[str, Any]
) -> None:
    seen = []

    def respond(*args: Any, **kwargs: Any) -> Response:
        seen.append(True)
        return Response({})

    monkeypatch.setattr(nott_system1, "_local_open", respond)
    with pytest.raises(System1Unavailable) as failure:
        ask(
            "private-state-marker",
            {"private-id": question},
            endpoint="http://localhost:8080/v1/chat/completions",
            model="local-test",
            timeout=15,
            protocol="openai-compatible",
        )
    assert not seen
    assert "private" not in str(failure.value)


def test_local_request_uses_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def respond(request: Any, **kwargs: Any) -> Response:
        seen.append(request)
        return Response({"q": {"type": "noul", "noul": 0.25}})

    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "unrelated-cloud-key")
    monkeypatch.setattr(nott_system1, "_local_open", respond)
    result = ask(
        "data",
        {"q": {"type": "noul", "instructions": "Does the statement hold?"}},
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="local-test",
        timeout=1,
        protocol="openai-compatible",
    )
    assert result["q"].value == 0.25
    assert seen[0].get_header("Authorization") is None
    assert "unrelated-cloud-key" not in seen[0].data.decode()
    assert json.loads(seen[0].data)["messages"]


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"q": {"type": "noul", "noul": True}},
        {"q": {"type": "noul", "noul": "0.5"}},
        {"q": {"type": "noul", "noul": float("nan")}},
        {"q": {"type": "noul", "noul": 1.1}},
        {"q": {"type": "choice", "choice": "yes"}},
    ],
)
def test_local_malformed_answer_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch, answers: object
) -> None:
    monkeypatch.setattr(nott_system1, "_local_open", lambda *a, **k: Response(answers))
    with pytest.raises(System1Unavailable, match="answer"):
        ask(
            "data",
            {"q": {"type": "noul", "instructions": "Check the statement."}},
            endpoint="http://localhost:8080/v1/chat/completions",
            model="local-test",
            timeout=1,
            protocol="openai-compatible",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cloud.invalid/v1/chat/completions",
        "http://192.0.2.1/v1/chat/completions",
        "https://127.0.0.1/v1/chat/completions",
        "http://user:password@localhost/v1/chat/completions",
        "http://localhost/v1/chat/completions?token=private",
        "http://localhost/v1/chat/completions#private",
    ],
)
def test_local_route_refuses_remote_endpoint(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("remote request")

    monkeypatch.setattr(nott_system1, "_local_open", forbidden)
    with pytest.raises(System1Unavailable, match="loopback"):
        ask(
            "data",
            {"q": {"type": "noul"}},
            endpoint=endpoint,
            model="local-test",
            timeout=1,
            protocol="openai-compatible",
        )


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            {
                "type": "choice",
                "criteria": {
                    "yes": "The statement holds.",
                    "no": "The statement fails.",
                },
            },
            {
                "type": "choice",
                "choice": "yes",
                "probabilities": {"yes": 0.8, "no": 0.2},
                "confidence": 0.9,
            },
        ),
        ({"type": "score"}, {"type": "score", "score": 42, "confidence": 0.9}),
    ],
)
def test_local_choice_and_score(
    monkeypatch: pytest.MonkeyPatch, question: dict[str, Any], answer: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        nott_system1, "_local_open", lambda *a, **k: Response({"q": answer})
    )
    result = ask(
        "data",
        {"q": question},
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="local-test",
        timeout=1,
        protocol="openai-compatible",
    )
    assert result["q"].value == answer[answer["type"]]
    assert result["q"].confidence == 0.9


@pytest.mark.parametrize(
    "answer",
    [
        {
            "type": "choice",
            "choice": "other",
            "probabilities": {"yes": 1},
            "confidence": 1,
        },
        {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": 0.4},
            "confidence": 1,
        },
        {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": 1, "extra": 0},
            "confidence": 1,
        },
        {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": "1"},
            "confidence": 1,
        },
        {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": float("nan")},
            "confidence": 1,
        },
        {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": 1},
            "confidence": True,
        },
        {
            "type": "choice",
            "choice": "yes",
            "probabilities": {"yes": 1},
            "confidence": float("inf"),
        },
    ],
)
def test_local_choice_rejects_invalid_distribution(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        nott_system1, "_local_open", lambda *a, **k: Response({"q": answer})
    )
    with pytest.raises(System1Unavailable, match="answer"):
        ask(
            "data",
            {"q": {"type": "choice", "criteria": {"yes": "The statement holds."}}},
            endpoint="http://127.0.0.1:8080/v1/chat/completions",
            model="local-test",
            timeout=1,
            protocol="openai-compatible",
        )


@pytest.mark.parametrize("value", [True, "42", None, float("nan"), float("inf")])
def test_local_score_rejects_invalid_numbers(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.setattr(
        nott_system1,
        "_local_open",
        lambda *a, **k: Response(
            {"q": {"type": "score", "score": value, "confidence": 1}}
        ),
    )
    with pytest.raises(System1Unavailable, match="score is not finite"):
        ask(
            "data",
            {"q": {"type": "score"}},
            endpoint="http://localhost:8080/v1/chat/completions",
            model="local-test",
            timeout=15,
            protocol="openai-compatible",
        )


def test_local_response_size_remains_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class OversizedResponse(Response):
        def read(self, size: int = -1) -> bytes:
            assert size == 1_000_001
            return b" " * size

    monkeypatch.setattr(
        nott_system1, "_local_open", lambda *a, **k: OversizedResponse(None)
    )
    with pytest.raises(System1Unavailable, match="response exceeds the size limit"):
        ask(
            "data",
            {"q": {"type": "noul"}},
            endpoint="http://localhost:8080/v1/chat/completions",
            model="local-test",
            timeout=15,
            protocol="openai-compatible",
        )


def test_local_transport_refuses_redirects_and_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    class Opener:
        def open(self, request: Any, *, timeout: float) -> object:
            return request

    def build(*handlers: Any) -> Opener:
        seen.extend(handlers)
        return Opener()

    monkeypatch.setattr(nott_system1.urllib.request, "build_opener", build)
    request = nott_system1.urllib.request.Request(
        "http://127.0.0.1:8080/v1/chat/completions"
    )
    assert nott_system1._local_open(request, timeout=1) is request
    assert seen[0].proxies == {}
    with pytest.raises(System1Unavailable, match="redirect"):
        seen[1].redirect_request(None, None, 302, "Found", {}, "https://cloud.invalid")
