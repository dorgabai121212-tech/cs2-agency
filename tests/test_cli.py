"""CLI via typer's CliRunner (mock LLM, dry run, no network)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agency.cli import app

runner = CliRunner()


def run(*args: str):
    result = runner.invoke(app, list(args), catch_exceptions=False)
    return result


def test_help_is_hebrew():
    result = run("--help")
    assert result.exit_code == 0
    assert "סוכנות" in result.output


def test_init_creates_config_env_and_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENCY_MOCK_LLM", "1")
    monkeypatch.delenv("AGENCY_DATA_DIR", raising=False)
    result = run("init")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "data" / "videos").is_dir()
    assert (tmp_path / "data" / "agency.db").exists()

    # a second init never overwrites .env (secrets) or config.yaml without --force
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=keep-me\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("dry_run: true\nchannel: {name: mine}\n",
                                          encoding="utf-8")
    assert run("init").exit_code == 0
    assert "keep-me" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "mine" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_status(project_dir):
    result = run("status")
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "מנכ\"ל" in result.output
    assert "תקציב" in result.output


def test_budget_expense_income(project_dir, db):
    assert run("expense", "50", "ads", "קידום").exit_code == 0
    assert run("income", "120.5", "adsense", "תשלום").exit_code == 0
    totals = db.month_totals(__import__("agency.staff.finance", fromlist=["x"]).current_month())
    assert totals == {"expense": 50.0, "income": 120.5, "net": 70.5}
    result = run("budget")
    assert result.exit_code == 0
    assert "50" in result.output and "120.5" in result.output


@pytest.mark.parametrize("amount", ["0", "-3"])
def test_expense_rejects_non_positive(project_dir, db, amount):
    result = runner.invoke(app, ["expense", "--", amount, "ads", "x"])
    assert result.exit_code != 0
    assert db.list_ledger() == []


def test_report(project_dir):
    result = run("report")
    assert result.exit_code == 0
    assert "דוח" in result.output


def test_platforms(project_dir):
    result = run("platforms")
    assert result.exit_code == 0, result.output
    assert "youtube" in result.output and "discord" in result.output


def test_approvals_empty(project_dir):
    result = run("approvals")
    assert result.exit_code == 0
    assert "אין אישורים" in result.output


def test_upload_approve_flow(project_dir, db, tiny_video, no_network):
    result = run("upload", str(tiny_video), "--title", "קלאץ' 1v3")
    assert result.exit_code == 0, result.output
    pending = db.list_approvals("pending")
    publish = [a for a in pending if a.kind == "publish"]
    assert publish
    aid = publish[0].id
    assert f"agency approve {aid}" in result.output

    listed = run("approvals")
    assert listed.exit_code == 0 and re.search(rf"\b{aid}\b", listed.output)

    approved = run("approve", str(aid), "--note", "מעולה", "--now")
    assert approved.exit_code == 0, approved.output
    assert db.get_approval(aid).status == "executed"
    posts = db.list_posts(publish[0].payload["video_id"])
    assert posts and all(p["status"] == "published" for p in posts)
    assert all(p["external_id"].startswith("dryrun-") for p in posts)

    again = runner.invoke(app, ["approve", str(aid)])
    assert again.exit_code == 1


def test_reject(project_dir, db):
    from agency.models import Approval

    aid = db.add_approval(Approval(id=None, kind="spend", title="מודעה", cost_ils=10,
                                   requested_by="promoter"))
    result = run("reject", str(aid), "--note", "לא")
    assert result.exit_code == 0, result.output
    assert db.get_approval(aid).status == "rejected"


def test_approve_unknown(project_dir):
    result = runner.invoke(app, ["approve", "4242"])
    assert result.exit_code == 1
    assert "לא נמצא" in result.output


def test_tick_and_brief(project_dir):
    assert run("tick").exit_code == 0
    brief = run("brief")
    assert brief.exit_code == 0, brief.output
    assert brief.output.strip()
