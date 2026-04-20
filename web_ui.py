import base64
import io
import json
import os
import shutil
from contextlib import redirect_stdout
from datetime import datetime

import frontmatter
import markdown
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename

from agents.automation.data_analyst import DataAnalystAgent
from agents.automation.daily_report_writer import DailyReportWriter
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
DEFAULT_REPORT_HISTORY_DIR = os.path.expanduser(os.getenv("REPORT_HISTORY_DIR") or "~/OneDrive/Work/工作日报")
DEFAULT_REPORT_OUTPUT_DIR = os.path.expanduser(os.getenv("REPORT_DRAFT_DIR") or "~/OneDrive/Work/工作日报/草稿")
DEFAULT_REPORT_TEMPLATE_PATH = os.path.expanduser(
    os.getenv("REPORT_TEMPLATE_PATH") or "~/OneDrive/Work/工作日报/草稿/工作日报-张丕哲-模板.md"
)
DEFAULT_REPORT_SOURCE_CONFIG = os.path.join(ROOT_DIR, "config", "report_topic_sources.json")


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


def _copy_to_output_dir(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = os.path.basename(file_path)
    target_path = os.path.join(OUTPUT_DIR, filename)
    if os.path.abspath(file_path) == os.path.abspath(target_path):
        return filename
    shutil.copy2(file_path, target_path)
    return filename


def _default_report_form_data():
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "history_dir": DEFAULT_REPORT_HISTORY_DIR,
        "output_dir": DEFAULT_REPORT_OUTPUT_DIR,
        "template_path": DEFAULT_REPORT_TEMPLATE_PATH,
        "reference_limit": 5,
        "novelty_window": 10,
        "count": 5,
        "source_config": DEFAULT_REPORT_SOURCE_CONFIG,
        "topic_suggestions": [],
        "topic_suggestions_payload": "",
        "selected_topic_id": "",
    }


def _encode_topic_suggestions(topics):
    raw = json.dumps(topics or [], ensure_ascii=False)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_topic_suggestions(payload):
    if not payload:
        return []
    try:
        raw = base64.b64decode(payload.encode("ascii")).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return []


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


def _daily_report(form_data):
    action = form_data.get("report_action") or "suggest_topics"
    llm_provider = None
    if os.getenv("DEEPSEEK_API_KEY"):
        llm_provider = LLMProvider(os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_MODEL"))

    agent = DailyReportWriter(llm_provider)
    payload = {
        "mode": "suggest_topics" if action == "suggest_topics" else "generate_report",
        "date": form_data.get("date"),
        "subject": form_data.get("subject"),
        "work_items": form_data.get("work_items"),
        "metrics": form_data.get("metrics"),
        "tech_thoughts": form_data.get("tech_thoughts"),
        "issues": form_data.get("issues"),
        "next_steps": form_data.get("next_steps"),
        "extra_requirements": form_data.get("extra_requirements"),
        "history_dir": form_data.get("history_dir") or DEFAULT_REPORT_HISTORY_DIR,
        "output_dir": form_data.get("output_dir") or DEFAULT_REPORT_OUTPUT_DIR,
        "template_path": form_data.get("template_path") or DEFAULT_REPORT_TEMPLATE_PATH,
        "reference_limit": int(form_data.get("reference_limit") or 5),
        "novelty_window": int(form_data.get("novelty_window") or 10),
        "count": int(form_data.get("count") or 5),
        "source_config": form_data.get("source_config") or DEFAULT_REPORT_SOURCE_CONFIG,
        "selected_topic_id": form_data.get("selected_topic_id") or "",
        "selected_topic_context": form_data.get("selected_topic_context") or "",
    }

    if action == "generate_report" and not payload["selected_topic_context"]:
        suggestions_payload = form_data.get("topic_suggestions_payload") or ""
        selected_topic_id = payload["selected_topic_id"]
        suggestions = _decode_topic_suggestions(suggestions_payload)
        form_data["topic_suggestions"] = suggestions
        if not selected_topic_id:
            return {
                "success": False,
                "title": "工作日报助手",
                "message": "请先从候选题里选一个，再生成日报。",
                "logs": "",
                "content": "",
                "rendered_content": "",
                "download_name": None,
                "download_label": "",
                "render_mode": "text",
            }
        for topic in suggestions:
            if topic.get("id") == selected_topic_id:
                payload["selected_topic_context"] = topic
                break
        if not payload["selected_topic_context"]:
            return {
                "success": False,
                "title": "工作日报助手",
                "message": "未找到你选中的题目，请重新生成候选题。",
                "logs": "",
                "content": "",
                "rendered_content": "",
                "download_name": None,
                "download_label": "",
                "render_mode": "text",
            }

    result, logs = _capture_output(
        agent.run,
        payload,
    )

    if not isinstance(result, dict):
        return {
            "success": False,
            "title": "工作日报助手",
            "message": "生成失败，请检查输入。",
            "logs": logs,
            "content": "",
            "rendered_content": "",
            "download_name": None,
            "download_label": "",
            "render_mode": "text",
        }

    if action == "suggest_topics":
        json_name = _copy_to_output_dir(result.get("topic_suggestions_json_path"))
        markdown_name = _copy_to_output_dir(result.get("topic_suggestions_markdown_path"))
        form_data["topic_suggestions"] = result.get("topic_suggestions", [])
        form_data["topic_suggestions_payload"] = _encode_topic_suggestions(result.get("topic_suggestions", []))
        if not form_data.get("selected_topic_id") and result.get("topic_suggestions"):
            form_data["selected_topic_id"] = result["topic_suggestions"][0]["id"]
        log_lines = []
        if logs:
            log_lines.append(logs)
        if result.get("external_logs"):
            log_lines.append("外部来源日志：\n" + "\n".join(result["external_logs"]))
        if result.get("references"):
            log_lines.append("参考历史日报：\n" + "\n".join(os.path.basename(item["path"]) for item in result["references"]))
        if result.get("topic_suggestions_json_path"):
            log_lines.append(f"JSON 已保存到：{result['topic_suggestions_json_path']}")
        if result.get("topic_suggestions_markdown_path"):
            log_lines.append(f"Markdown 已保存到：{result['topic_suggestions_markdown_path']}")
        if json_name:
            log_lines.append(f"工作区副本：{os.path.join(OUTPUT_DIR, json_name)}")
        if markdown_name:
            log_lines.append(f"工作区副本：{os.path.join(OUTPUT_DIR, markdown_name)}")

        message = f"已生成 {len(result.get('topic_suggestions', []))} 个候选题，请选一个继续。"
        if not result.get("used_llm"):
            message += "（当前未检测到 LLM Key，候选题由本地规则生成）"

        return {
            "success": True,
            "title": "工作日报助手",
            "message": message,
            "logs": "\n\n".join(log_lines),
            "content": "",
            "rendered_content": "",
            "download_name": markdown_name or json_name,
            "download_label": "下载选题清单",
            "render_mode": "text",
        }

    markdown_name = _copy_to_output_dir(result.get("markdown_path"))
    docx_name = _copy_to_output_dir(result.get("docx_path"))
    ref_lines = [os.path.basename(item["path"]) for item in result.get("references", [])]
    log_lines = []
    if logs:
        log_lines.append(logs)
    if ref_lines:
        log_lines.append("参考历史日报：\n" + "\n".join(ref_lines))
    if result.get("selected_topic"):
        log_lines.append(f"已选题目：{result['selected_topic']['title']}（{result['selected_topic']['lane_label']}）")
    if result.get("markdown_path"):
        log_lines.append(f"Markdown 已保存到：{result['markdown_path']}")
    if result.get("docx_path"):
        log_lines.append(f"DOCX 已保存到：{result['docx_path']}")
    elif result.get("docx_error"):
        log_lines.append(f"DOCX 导出失败：{result['docx_error']}")
    if markdown_name:
        log_lines.append(f"工作区副本：{os.path.join(OUTPUT_DIR, markdown_name)}")
    if docx_name:
        log_lines.append(f"工作区副本：{os.path.join(OUTPUT_DIR, docx_name)}")

    message = "日报初稿已生成"
    if not result.get("used_llm"):
        message += "（当前未检测到 LLM Key，已使用本地模板兜底）"

    return {
        "success": True,
        "title": "工作日报助手",
        "message": message,
        "logs": "\n\n".join(log_lines),
        "content": result.get("content", ""),
        "rendered_content": _render_markdown(result.get("content", "")),
        "download_name": docx_name or markdown_name,
        "download_label": "下载 DOCX" if docx_name else "下载 Markdown",
        "render_mode": "markdown" if result.get("content") else "text",
    }


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

    @app.get("/")
    def index():
        return render_template(
            "home.html",
            skills=_list_skills(),
            active_tab="daily-report",
            result=None,
            form_data=_default_report_form_data(),
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

    @app.post("/run/daily-report")
    def run_daily_report():
        form_data = _default_report_form_data()
        form_data.update(
            {
                "date": (request.form.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d"),
                "subject": (request.form.get("subject") or "").strip(),
                "work_items": request.form.get("work_items") or "",
                "metrics": request.form.get("metrics") or "",
                "tech_thoughts": request.form.get("tech_thoughts") or "",
                "issues": request.form.get("issues") or "",
                "next_steps": request.form.get("next_steps") or "",
                "extra_requirements": request.form.get("extra_requirements") or "",
                "history_dir": (request.form.get("history_dir") or "").strip() or DEFAULT_REPORT_HISTORY_DIR,
                "output_dir": (request.form.get("output_dir") or "").strip() or DEFAULT_REPORT_OUTPUT_DIR,
                "template_path": (request.form.get("template_path") or "").strip() or DEFAULT_REPORT_TEMPLATE_PATH,
                "reference_limit": (request.form.get("reference_limit") or "").strip() or "5",
                "novelty_window": (request.form.get("novelty_window") or "").strip() or "10",
                "count": (request.form.get("count") or "").strip() or "5",
                "source_config": (request.form.get("source_config") or "").strip() or DEFAULT_REPORT_SOURCE_CONFIG,
                "report_action": (request.form.get("report_action") or "").strip() or "suggest_topics",
                "topic_suggestions_payload": request.form.get("topic_suggestions_payload") or "",
                "selected_topic_id": (request.form.get("selected_topic_id") or "").strip(),
                "selected_topic_context": request.form.get("selected_topic_context") or "",
            }
        )
        if form_data["topic_suggestions_payload"]:
            form_data["topic_suggestions"] = _decode_topic_suggestions(form_data["topic_suggestions_payload"])
        result = _daily_report(form_data)
        return render_template(
            "home.html",
            skills=_list_skills(),
            active_tab="daily-report",
            result=result,
            form_data=form_data,
        )

    @app.get("/download/<path:filename>")
    def download_output(filename):
        safe_path = os.path.abspath(os.path.join(OUTPUT_DIR, filename))
        output_root = os.path.abspath(OUTPUT_DIR)
        if os.path.commonpath([safe_path, output_root]) != output_root or not os.path.exists(safe_path):
            return "文件不存在", 404
        return send_file(safe_path, as_attachment=True)

    return app
