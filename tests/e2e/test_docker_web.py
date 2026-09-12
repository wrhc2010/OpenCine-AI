"""Browser-only acceptance flow for the three-container Docker deployment.

Run this test against a running Compose stack with Playwright browsers installed.
The test never calls the backend API directly; every assertion and action goes
through the WebUI at ``OPENCINE_E2E_BASE_URL``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect  # noqa: E402


BASE_URL = os.getenv("OPENCINE_E2E_BASE_URL", "http://localhost:3000")


def _model_group() -> dict[str, str]:
    """Read the attached model group without ever logging secret values."""
    path = os.getenv("OPENCINE_E2E_MODEL_GROUP_FILE")
    values: dict[str, str] = {}
    if path:
        raw = Path(path).read_text(encoding="utf-8")
        try:
            candidate = json.loads(raw)
            if isinstance(candidate, dict):
                values.update({str(key): str(value) for key, value in candidate.items()})
        except json.JSONDecodeError:
            for line in raw.splitlines():
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in (
        "AGNES_API_KEY",
        "AGNES_BACKUP_API_KEY",
        "OPENAI_BASE_URL",
        "DIRECTOR_LLM_MODEL",
        "DIRECTOR_VLM_MODEL",
        "VIDEO_MODEL",
    ):
        if os.getenv(key):
            values[key] = os.environ[key]
    required = ("AGNES_API_KEY", "OPENAI_BASE_URL", "DIRECTOR_LLM_MODEL", "DIRECTOR_VLM_MODEL", "VIDEO_MODEL")
    missing = [key for key in required if not values.get(key)]
    if missing:
        pytest.fail("模型组缺少必要配置：" + ", ".join(missing))
    return values


def _fill_first_unanswered_question(page: Page) -> None:
    questions = page.locator(".question")
    for index in range(questions.count()):
        question = questions.nth(index)
        radios = question.locator("input[type='radio']")
        if radios.count():
            radios.first.check()
        else:
            question.locator("input").first.fill("接受默认值")


@pytest.mark.e2e
def test_real_docker_web_flow(page: Page, tmp_path: Path) -> None:
    model = _model_group()
    username = os.getenv("OPENCINE_E2E_USERNAME", "e2e-admin")
    password = os.getenv("OPENCINE_E2E_PASSWORD", "e2e-password-123")

    page.goto(BASE_URL, wait_until="networkidle")
    if page.get_by_role("heading", name="设置管理员账号").count():
        page.get_by_label("用户名").fill(username)
        page.get_by_role("textbox", name="密码", exact=True).fill(password)
        page.get_by_role("textbox", name="确认密码", exact=True).fill(password)
        page.get_by_role("button", name="完成初始化").click()
    elif page.get_by_role("heading", name="登录制作空间").count():
        page.get_by_label("用户名").fill(username)
        page.get_by_role("textbox", name="密码", exact=True).fill(password)
        page.get_by_role("button", name="登录").click()

    page.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).click()
    page.get_by_label("视频 Provider").fill("agnes")
    page.get_by_label("视频模型").fill(model["VIDEO_MODEL"])
    page.get_by_label("LLM 模型").fill(model["DIRECTOR_LLM_MODEL"])
    page.get_by_label("VLM 模型").fill(model["DIRECTOR_VLM_MODEL"])
    page.get_by_label("模型 API 地址").fill(model["OPENAI_BASE_URL"])
    page.get_by_label("Agnes API Key").fill(model["AGNES_API_KEY"])
    if model.get("AGNES_BACKUP_API_KEY"):
        page.get_by_label("AGNES_BACKUP_API_KEY").fill(model["AGNES_BACKUP_API_KEY"])
    page.get_by_role("button", name="保存设置").click()
    expect(page.get_by_text("设置已保存。", exact=True)).to_be_visible()

    page.get_by_role("navigation", name="主导航").get_by_role("button", name="新建项目", exact=True).click()
    page.get_by_label("项目名称").fill("Agnes 浏览器验收")
    page.get_by_label("原始创作提示词").fill("一位快递员在清晨把一封信交还给收件人，镜头稳定、光线自然。")
    page.get_by_label("总时长（秒）").fill("3.375")
    page.get_by_label("单镜头时长（秒）").fill("3.375")
    page.get_by_label("最大镜头数").fill("1")
    page.get_by_label("帧率").select_option("24")
    page.get_by_role("button", name="进入需求澄清").click()

    for _ in range(5):
        expect(page.get_by_role("heading", name="需求澄清")).to_be_visible()
        if page.get_by_role("heading", name="需求信息已经足够").count():
            page.get_by_role("button", name="查看计划").click()
            break
        _fill_first_unanswered_question(page)
        page.get_by_role("button", name="提交回答").click()
        page.wait_for_timeout(500)
        if page.get_by_role("heading", name="计划审核").count():
            break

    expect(page.get_by_role("heading", name="计划审核")).to_be_visible()
    if page.get_by_role("button", name="生成计划").count():
        page.get_by_role("button", name="生成计划").click()
    expect(page.get_by_role("button", name="审核并开始制作")).to_be_visible()
    page.get_by_role("button", name="审核并开始制作").click()
    page.get_by_role("button", name="开始制作").click()

    expect(page.get_by_text("制作任务已排队，后端 Worker 会继续处理。", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="查看验收")).to_be_visible(timeout=900_000)
    page.get_by_role("button", name="查看验收").click()
    expect(page.get_by_role("heading", name="镜头与证据")).to_be_visible()
    page.get_by_role("button", name="进入成片预览").click()

    video = page.locator("video")
    expect(video).to_be_visible()
    expect.poll(lambda: video.evaluate("element => element.readyState"), timeout=120_000).to_be_greater_than(0)
    target = tmp_path / "delivery.mp4"
    with page.expect_download(timeout=120_000) as download_info:
        page.get_by_role("link", name="下载 MP4").click()
    download_info.value.save_as(target)
    assert target.stat().st_size > 0

    page.get_by_role("button", name="确认交付").click()
    expect(page.get_by_text("已交付", exact=True)).to_be_visible()
    page.get_by_role("button", name="创建新版本").click()
    expect(page.get_by_role("heading", name="需求澄清")).to_be_visible()
