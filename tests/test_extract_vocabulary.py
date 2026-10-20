"""Golden language cases: literal examples, never imported production lexicons."""

from nmemory.extract import CandidateKind, extract


def test_bilingual_label_corpus() -> None:
    families = [
        ("task", "todo-word", "fixme|pendencia|pendência|pendente|tarefa|task|todo"),
        ("task", "todo-label", "a fazer"),
        (
            "epic",
            "epic-label",
            "campaign|campanha|epic|epico|initiative|iniciativa|marco|milestone|workstream|épico",
        ),
        ("plan", "plan-label", "plan|plano"),
        (
            "brainstorm",
            "idea-label",
            "brainstorm|hipotese|hipótese|hypothesis|idea|ideia",
        ),
        (
            "doc",
            "doc-label",
            "doc|docs|documentacao|documentation|documentação|guia|guide|manual|readme|reference|referencia|referência|runbook",
        ),
        (
            "procedure",
            "procedure-label",
            "how-to|howto|passo a passo|passos|procedimento|procedure|steps",
        ),
        ("constraint", "constraint-label", "constraint|restricao|restrição"),
        ("capability", "capability-label", "capability|capacidade"),
        ("failure_pattern", "failure-label", "failure|falha|sintoma|symptom"),
    ]
    for kind, cue, labels in families:
        for label in labels.split("|"):
            text = f"{label}: preserve receipts"
            result = extract(text)
            assert [item.to_dict() for item in result] == [
                {"content": text, "kind": kind, "cue": f"{cue}:'{label}'"}
            ], text


def test_bilingual_decision_corpus() -> None:
    for verb in [
        "adopted",
        "adotada",
        "adotado",
        "adotamos",
        "chose",
        "chosen",
        "congelado",
        "congelada",
        "decided",
        "decidida",
        "decidido",
        "decidimos",
        "decidiu",
        "decision",
        "decisao",
        "decisão",
        "escolhemos",
        "escolhida",
        "escolhido",
        "frozen",
        "locked",
        "optamos",
        "opted",
        "ratificado",
        "ratificada",
        "ratified",
        "rejeitado",
        "rejeitada",
        "rejected",
        "settled",
        "superseded",
        "supersedes",
        "travado",
        "travada",
        "vetado",
        "vetada",
        "veto",
        "vetoed",
    ]:
        text = f"The team {verb} this choice"
        result = extract(text)
        assert len(result) == 1, text
        assert result[0].kind is CandidateKind.DECISION, text
        assert result[0].content == text
        assert result[0].cue == f"decision-word:'{verb}'"
    for phrase in [
        "we will use",
        "going with",
        "instead of",
        "vamos usar",
        "em vez de",
        "ao invés de",
        "ao inves de",
    ]:
        text = f"{phrase} the database"
        assert extract(text)[0].cue == f"decision-phrase:'{phrase}'"


def test_bilingual_action_corpus() -> None:
    for verb in [
        "abra",
        "add",
        "adicione",
        "always",
        "aplique",
        "apply",
        "atualize",
        "avoid",
        "build",
        "call",
        "check",
        "commit",
        "configure",
        "copy",
        "create",
        "crie",
        "delete",
        "deploy",
        "disable",
        "do",
        "don't",
        "enable",
        "ensure",
        "evite",
        "execute",
        "exija",
        "export",
        "exporte",
        "faca",
        "faça",
        "fetch",
        "garanta",
        "gere",
        "import",
        "importe",
        "inicie",
        "inject",
        "install",
        "instale",
        "jamais",
        "keep",
        "mantenha",
        "move",
        "nao",
        "não",
        "never",
        "nunca",
        "open",
        "pare",
        "pin",
        "prefer",
        "prefira",
        "pull",
        "push",
        "rebase",
        "register",
        "reinicie",
        "remova",
        "remove",
        "restart",
        "rode",
        "run",
        "sempre",
        "serialize",
        "set",
        "start",
        "stop",
        "strip",
        "suba",
        "test",
        "teste",
        "update",
        "use",
        "valide",
        "validate",
        "verifique",
        "verify",
        "wrap",
    ]:
        text = f"{verb} the package"
        result = extract(text)
        assert len(result) == 1, text
        assert result[0].kind is CandidateKind.PROCEDURE, text
        assert result[0].content == text
        assert result[0].cue == f"imperative-opener:'{verb}'"


def test_bilingual_questions_and_instructions() -> None:
    for phrase in [
        "could we",
        "should we",
        "what if",
        "why not",
        "deveriamos",
        "deveríamos",
        "e se",
        "poderiamos",
        "poderíamos",
        "por que nao",
        "por que não",
        "que tal",
    ]:
        text = f"{phrase} change the database?"
        assert extract(text)[0].cue == f"open-question:'{phrase}'"
    for phrase in ["how to", "steps to", "como fazer", "passos para"]:
        text = f"{phrase} install the package"
        assert extract(text)[0].cue == f"how-to-opener:'{phrase}'"
    for phrase in ["use when", "use quando"]:
        text = f"{phrase} investigating latency"
        assert extract(text)[0].cue == f"use-when:'{phrase}'"


def test_bilingual_prohibition_and_failure_frames() -> None:
    for phrase in [
        "must not",
        "must never",
        "não pode",
        "nao pode",
        "não podem",
        "nao podem",
        "não deve",
        "nao deve",
        "não devem",
        "nao devem",
    ]:
        text = f"Agents {phrase} publish secrets"
        result = extract(text)
        assert result[0].kind is CandidateKind.CONSTRAINT
        assert result[0].cue == f"prohibition:'{phrase}'"
    for phrase in [
        "fails with",
        "fails when",
        "breaks with",
        "breaks when",
        "falha com",
        "falham com",
        "falha quando",
        "falham quando",
        "quebra com",
        "quebram com",
        "quebra quando",
        "quebram quando",
    ]:
        text = f"The system {phrase} credentials expire"
        result = extract(text)
        assert result[0].kind is CandidateKind.FAILURE_PATTERN
        assert result[0].cue == f"failure-frame:'{phrase}'"


def test_bilingual_declarative_and_prescriptive_frames() -> None:
    for phrase in [
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "lives at",
        "lives in",
        "uses",
        "requires",
        "depends on",
        "defaults to",
        "runs on",
        "contains",
        "belongs to",
        "means",
        "maps to",
        "points to",
        "equals",
        "=",
        "->",
        "=>",
        "owns",
        "must",
        "é",
        "são",
        "foi",
        "foram",
        "era",
        "eram",
        "fica em",
        "ficam em",
        "usa",
        "usam",
        "tem",
        "têm",
        "requer",
        "requerem",
        "depende de",
        "dependem de",
        "significa",
        "significam",
        "aponta para",
        "apontam para",
        "roda em",
        "rodam em",
        "pertence a",
        "pertencem a",
        "contém",
        "contêm",
        "deve",
        "devem",
    ]:
        text = f"/config {phrase} configured"
        result = extract(text)
        assert result[0].kind is CandidateKind.FACT, text
        assert result[0].cue == f"declarative:'{phrase}' entity:'/config'", text
    for word in ["never", "always", "nunca", "sempre", "jamais"]:
        text = f"Agents {word} publish"
        assert extract(text)[0].cue == f"prescriptive:'{word}'"
