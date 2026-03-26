import io
import os
from contextlib import redirect_stdout
from datetime import datetime

import frontmatter
import markdown
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename

from agents.automation.data_analyst import DataAnalystAgent
from agents.dev_assistant.work_estimator import WorkEstimator
from agents.project_management.workhour_fetcher import WorkhourFetcher
from core.providers.data_provider import DataProvider
from core.providers.email_provider import EmailProvider
from core.providers.jira_provider import JiraProvider
from core.providers.llm_provider import LLMProvider

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(ROOT_DIR, "agents", "automation", "skills")
UPLOAD_DIR = os.path.join(ROOT_DIR, "data", "uploads")
OUTPUT_DIR = os.path.join(ROOT_DIR, os.getenv("OUTPUT_DIR") or "data/output")
HISTORY_DIR = os.path.join(ROOT_DIR, os.getenv("HISTORY_DIR") or "data/output")


def _capture_output(func, *args, **kwargs):
    stream = io.StringIO()
    with redirect_stdout(stream):
        result = func(*args, **kwargs)
    return result, stream.getvalue().strip()


def _is_error(result):
    return isinstance(result, str) and result.startswith("Error:")


def _list_skills():
    skills = []
    if not os.path.isdir(SKILL_DIR):
        return skills

    for filename in sorted(os.listdir(SKILL_DIR)):
        if not filename.endswith(".md"):
            continue
        skill_path = os.path.join(SKILL_DIR, filename)
        skill_data = frontmatter.load(skill_path)
        skills.append(
            {
                "value": os.path.splitext(filename)[0],
                "name": skill_data.metadata.get("name", filename),
                "description": skill_data.metadata.get("description", ""),
                "default_email": skill_data.metadata.get("email_to", ""),
            }
        )
    return skills


def _save_upload(file_storage):
    if not file_storage or not file_storage.filename:
        return None

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = secure_filename(file_storage.filename)
    saved_name = f"{timestamp}_{filename}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)
    file_storage.save(saved_path)
    return saved_path


def _save_markdown_output(prefix, content):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{timestamp}.md"
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)
    return filename


def _render_markdown(content):
    return markdown.markdown(
        content,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
    )


def _jira_fetch(project_key):
    if not project_key:
        return {
            "success": False,
            "title": "Jira 工时抓取",
            "message": "请先填写项目 Key。",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    missing = [key for key in ["JIRA_URL", "JIRA_USER", "JIRA_PASS"] if not os.getenv(key)]
    if missing:
        return {
            "success": False,
            "title": "Jira 工时抓取",
            "message": f"缺少环境变量: {', '.join(missing)}",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    provider = JiraProvider(os.getenv("JIRA_URL"), os.getenv("JIRA_USER"), os.getenv("JIRA_PASS"))
    agent = WorkhourFetcher(provider)
    result, logs = _capture_output(agent.run, project_key, OUTPUT_DIR)

    output_path = os.path.join(OUTPUT_DIR, f"{project_key}_story_summary.md")
    content = ""
    download_name = None
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as file:
            content = file.read()
        download_name = os.path.basename(output_path)

    return {
        "success": not _is_error(result),
        "title": "Jira 工时抓取",
        "message": result,
        "logs": logs,
        "content": content,
        "rendered_content": "",
        "download_name": download_name,
        "download_label": "下载输出文件",
        "render_mode": "text",
    }


def _estimate(story_text, story_file):
    if not story_text and not story_file:
        return {
            "success": False,
            "title": "工时预估",
            "message": "请填写需求内容，或上传一个 Markdown 文件。",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    if not os.getenv("DEEPSEEK_API_KEY"):
        return {
            "success": False,
            "title": "工时预估",
            "message": "缺少环境变量: DEEPSEEK_API_KEY",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    content = story_text.strip() if story_text else ""
    if not content and story_file:
        with open(story_file, "r", encoding="utf-8") as file:
            content = file.read()

    provider = LLMProvider(os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_MODEL"))
    agent = WorkEstimator(provider)
    result, logs = _capture_output(agent.estimate, content, HISTORY_DIR)
    markdown_result = result if isinstance(result, str) else ""
    download_name = None
    rendered_content = ""

    if not _is_error(markdown_result) and markdown_result:
        download_name = _save_markdown_output("estimate", markdown_result)
        rendered_content = _render_markdown(markdown_result)

    return {
        "success": not _is_error(markdown_result),
        "title": "工时预估",
        "message": "预估已完成" if not _is_error(markdown_result) else markdown_result,
        "logs": logs,
        "content": markdown_result,
        "rendered_content": rendered_content,
        "download_name": download_name,
        "download_label": "下载 Markdown",
        "render_mode": "markdown" if rendered_content else "text",
    }


def _data_analyze(data_file, skill_name, to_email):
    if not data_file:
        return {
            "success": False,
            "title": "数据分析",
            "message": "请上传一个 Excel 或 CSV 文件。",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    if not skill_name:
        return {
            "success": False,
            "title": "数据分析",
            "message": "请先选择分析模板。",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    if not os.getenv("DEEPSEEK_API_KEY"):
        return {
            "success": False,
            "title": "数据分析",
            "message": "缺少环境变量: DEEPSEEK_API_KEY",
            "logs": "",
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    data_provider = DataProvider()
    llm_provider = LLMProvider(os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_MODEL"))
    email_provider = EmailProvider(
        os.getenv("SMTP_SERVER"),
        int(os.getenv("SMTP_PORT") or 465),
        os.getenv("SMTP_USER"),
        os.getenv("SMTP_PASS"),
    )

    agent = DataAnalystAgent(data_provider, llm_provider, email_provider)
    result, logs = _capture_output(agent.run, data_file, skill_name, to_email or None)

    return {
        "success": not _is_error(result),
        "title": "数据分析",
        "message": "分析已完成" if not _is_error(result) else result,
        "logs": logs,
        "content": result if isinstance(result, str) else "",
        "rendered_content": result if isinstance(result, str) and result.lstrip().startswith("<") else "",
        "download_name": None,
        "download_label": "",
        "render_mode": "html" if isinstance(result, str) and result.lstrip().startswith("<") else "text",
    }


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

    @app.get("/")
    def index():
        return render_template(
            "home.html",
            skills=_list_skills(),
            active_tab="jira-fetch",
            result=None,
            form_data={},
        )

    @app.post("/run/jira-fetch")
    def run_jira_fetch():
        project = (request.form.get("project") or "").strip()
        result = _jira_fetch(project)
        return render_template(
            "home.html",
            skills=_list_skills(),
            active_tab="jira-fetch",
            result=result,
            form_data={"project": project},
        )

    @app.post("/run/estimate")
    def run_estimate():
        story_text = request.form.get("story_text") or ""
        story_file = _save_upload(request.files.get("story_file"))
        result = _estimate(story_text, story_file)
        return render_template(
            "home.html",
            skills=_list_skills(),
            active_tab="estimate",
            result=result,
            form_data={"story_text": story_text},
        )

    @app.post("/run/data-analyze")
    def run_data_analyze():
        data_file = _save_upload(request.files.get("data_file"))
        skill_name = request.form.get("skill_name") or ""
        to_email = (request.form.get("to_email") or "").strip()
        result = _data_analyze(data_file, skill_name, to_email)
        return render_template(
            "home.html",
            skills=_list_skills(),
            active_tab="data-analyze",
            result=result,
            form_data={"skill_name": skill_name, "to_email": to_email},
        )

    @app.get("/download/<path:filename>")
    def download_output(filename):
        safe_path = os.path.abspath(os.path.join(OUTPUT_DIR, filename))
        output_root = os.path.abspath(OUTPUT_DIR)
        if os.path.commonpath([safe_path, output_root]) != output_root or not os.path.exists(safe_path):
            return "文件不存在", 404
        return send_file(safe_path, as_attachment=True)

    return app
