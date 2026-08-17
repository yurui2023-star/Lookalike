"""Contract checks for Lookalike detailed design v4.0 (SAD + Sales CDP schema)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZH = ROOT / "docs" / "Lookalike_Detailed_Design_v4.0_BatchScoring.html"
EN = ROOT / "docs" / "Lookalike_Detailed_Design_v4.0_BatchScoring_EN.html"
MD = ROOT / "docs" / "Lookalike_Detailed_Design_v4.0_BatchScoring.md"
DOCX = ROOT / "docs" / "Lookalike_Detailed_Design_v4.0_BatchScoring.docx"

CDP_FIELDS = [
    "Lookalike Model ID x Profile ID",
    "Profile ID",
    "Lookalike score",
    "Model ID",
    "Lookalike model",
    "product ID",
]

REQUIRED_BOTH = [
    "run_batch_id",
    "lookalike_key",
    "publish_status",
    "ClickHouse",
    "Smart Sales",
    "White-Collar Loan",
    "Home Easy Loan",
    "/api/v1/lookalike/runs",
    "AD-01",
    "leakage",
]

RETIRED_MUST_APPEAR = [
    "Segment",
    "upload",
]


def test_v4_design_docs_exist() -> None:
    assert ZH.is_file(), f"missing {ZH}"
    assert EN.is_file(), f"missing {EN}"
    assert MD.is_file(), f"missing {MD}"
    assert DOCX.is_file(), f"missing {DOCX}"
    assert DOCX.stat().st_size > 10_000


def test_v4_markdown_covers_cdp_contract() -> None:
    text = MD.read_text(encoding="utf-8")
    for field in CDP_FIELDS:
        assert field in text, f"MD missing CDP field: {field}"
    assert "{run_batch_id}x{profile_id}" in text
    assert "## 8. Sales CDP 结果 schema 与写入" in text
    assert "取消 Lookalike 前端" in text


def test_v4_zh_covers_sad_and_cdp_contract() -> None:
    text = ZH.read_text(encoding="utf-8")
    for field in CDP_FIELDS:
        assert field in text, f"ZH doc missing CDP field: {field}"
    for token in REQUIRED_BOTH:
        assert token in text, f"ZH doc missing required token: {token}"
    assert "取消" in text or "退役" in text
    assert "前端" in text
    assert "{run_batch_id}x{profile_id}" in text
    assert "50,000,000" not in text


def test_v4_en_covers_sad_and_cdp_contract() -> None:
    text = EN.read_text(encoding="utf-8")
    for field in CDP_FIELDS:
        assert field in text, f"EN doc missing CDP field: {field}"
    for token in REQUIRED_BOTH:
        assert token in text, f"EN doc missing required token: {token}"
    for token in RETIRED_MUST_APPEAR:
        assert token in text, f"EN doc should mention retired capability: {token}"
    assert "{run_batch_id}x{profile_id}" in text
    assert "No frontend" in text or "no frontend" in text.lower() or "without a frontend" in text.lower()
    assert "50,000,000" not in text


def test_v4_does_not_keep_production_training() -> None:
    zh = ZH.read_text(encoding="utf-8")
    en = EN.read_text(encoding="utf-8")
    assert "生产只加载" in zh or "不训练" in zh
    assert "does not" in en.lower() and "train" in en.lower()
    assert "不在生产自动训练" in zh
