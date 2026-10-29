"""Term sets: import parsing, dedupe and CRUD routes."""

import httpx

from asr.providers.base import Term
from server.routes.term_sets import merge_terms, parse_terms


def test_parse_formats() -> None:
    text = (
        "﻿# comment line\n"
        "FPGA\n"
        "\n"
        "Kubernetes,5\n"
        "芯片，4\n"
        "SoC\t2\tsystem on chip, integrated\n"
        "  CUDA , 50 , gpu \n"
        "trailing,\n"
    )
    terms, errors = parse_terms(text)
    assert errors == []
    assert terms == [
        Term(term="FPGA"),
        Term(term="Kubernetes", weight=5),
        Term(term="芯片", weight=4),
        Term(term="SoC", weight=2, note="system on chip, integrated"),
        Term(term="CUDA", weight=50, note="gpu"),
        Term(term="trailing"),
    ]


def test_parse_hotword_syntax() -> None:
    terms, errors = parse_terms(
        "Townfall | 汤泡 | 汤炮 ~~~ 泡面 | 面条, 4, game title\n"
        "Claude | 克劳德 | cloud\n"
        "傅平 ~~~ 工作 | 脱贫\n"
        "Plain\n"
    )
    assert not errors
    by = {t.term: t for t in terms}
    assert by["Townfall"].aliases == ["汤泡", "汤炮"]
    assert by["Townfall"].blacklist == ["泡面", "面条"]
    assert (by["Townfall"].weight, by["Townfall"].note) == (4, "game title")
    assert by["Claude"].aliases == ["克劳德", "cloud"] and by["Claude"].blacklist == []
    assert by["傅平"].aliases == [] and by["傅平"].blacklist == ["工作", "脱贫"]
    assert by["Plain"].aliases == [] and by["Plain"].weight == 3


def test_merge_terms_unions_aliases() -> None:
    merged = merge_terms(
        [
            Term(term="Townfall", aliases=["汤泡"], blacklist=["泡面"]),
            Term(term="townfall", aliases=["汤泡", "汤炮"], blacklist=["面条"]),
        ]
    )
    assert len(merged) == 1
    assert merged[0].aliases == ["汤泡", "汤炮"] and merged[0].blacklist == ["泡面", "面条"]


def test_parse_errors_report_line_numbers() -> None:
    text = "ok\nbad,abc\n,3\nweight,9\n" + "x" * 101 + "\n"
    terms, errors = parse_terms(text)
    assert [t.term for t in terms] == ["ok"]
    assert [e["loc"][-1] for e in errors] == [2, 3, 4, 5]
    assert all(e["msg"].startswith(f"line {e['loc'][-1]}:") for e in errors)
    assert errors[0]["input"] == "bad,abc"


def test_merge_terms_case_insensitive_max_weight() -> None:
    merged = merge_terms(
        [Term(term="FPGA", weight=2), Term(term="fpga", weight=5, note="n"), Term(term="SoC")]
    )
    assert merged == [Term(term="FPGA", weight=5, note="n"), Term(term="SoC")]


async def test_crud(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/term-sets")).json() == []
    resp = await client.post(
        "/api/term-sets",
        json={"name": " 芯片 ", "terms": [{"term": "FPGA"}, {"term": "fpga", "weight": 4}]},
    )
    assert resp.status_code == 201
    ts = resp.json()
    assert ts["name"] == "芯片"
    assert ts["terms"] == [
        {"term": "FPGA", "weight": 4, "note": None, "aliases": [], "blacklist": []}
    ]

    got = (await client.get(f"/api/term-sets/{ts['id']}")).json()
    assert got == ts

    resp = await client.put(
        f"/api/term-sets/{ts['id']}", json={"name": "chips", "terms": [{"term": "SoC"}]}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "chips"
    assert resp.json()["updated_at"] >= ts["updated_at"]
    assert [t["name"] for t in (await client.get("/api/term-sets")).json()] == ["chips"]

    assert (await client.delete(f"/api/term-sets/{ts['id']}")).status_code == 204
    assert (await client.get(f"/api/term-sets/{ts['id']}")).status_code == 404
    assert (await client.delete(f"/api/term-sets/{ts['id']}")).status_code == 404
    put = await client.put(f"/api/term-sets/{ts['id']}", json={"name": "x", "terms": []})
    assert put.status_code == 404


async def test_validation(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/term-sets", json={"name": "", "terms": []})).status_code == 422
    assert (
        await client.post("/api/term-sets", json={"name": "  ", "terms": []})
    ).status_code == 422
    bad_weight = {"name": "x", "terms": [{"term": "a", "weight": 7}]}
    assert (await client.post("/api/term-sets", json=bad_weight)).status_code == 422


async def test_import(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/term-sets/import", json={"name": "导入", "text": "FPGA,3\n# c\n芯片，5\nSoC\t2\tnote"}
    )
    assert resp.status_code == 201, resp.text
    assert [(t["term"], t["weight"]) for t in resp.json()["terms"]] == [
        ("FPGA", 3),
        ("芯片", 5),
        ("SoC", 2),
    ]


async def test_import_invalid_lines_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/term-sets/import", json={"name": "x", "text": "ok\nbad,zz\n"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "text", 2]
    assert "line 2" in detail[0]["msg"]
    assert (await client.get("/api/term-sets")).json() == []


async def test_import_too_many(client: httpx.AsyncClient) -> None:
    text = "\n".join(f"term{i}" for i in range(1001))
    resp = await client.post("/api/term-sets/import", json={"name": "big", "text": text})
    assert resp.status_code == 422
