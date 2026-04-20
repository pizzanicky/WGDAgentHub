import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

from core.base import BaseAgent


class DailyReportWriter(BaseAgent):
    def __init__(self, llm_provider=None):
        self.llm = llm_provider

    def run(self, payload):
        report_date = self._parse_report_date(payload.get("date"))
        history_dir = os.path.abspath(os.path.expanduser(payload["history_dir"]))
        output_dir = os.path.abspath(os.path.expanduser(payload["output_dir"]))
        os.makedirs(output_dir, exist_ok=True)

        template_path = payload.get("template_path") or ""
        template = self._load_template(template_path)
        references = self._load_history_samples(
            history_dir,
            report_date,
            int(payload.get("reference_limit") or 5),
        )

        content = self._generate_content(report_date, payload, template, references)
        base_name = f"工作日报-张丕哲-{report_date.year}.{report_date.month}.{report_date.day}"
        markdown_path = os.path.join(output_dir, f"{base_name}.md")

        with open(markdown_path, "w", encoding="utf-8") as file:
            file.write(content)

        docx_path, docx_error = self._export_docx(content, base_name, output_dir)

        return {
            "content": content,
            "markdown_path": markdown_path,
            "docx_path": docx_path,
            "docx_error": docx_error,
            "references": references,
            "used_llm": bool(self.llm and getattr(self.llm, "api_key", None)),
        }

    def _generate_content(self, report_date, payload, template, references):
        if self.llm and getattr(self.llm, "api_key", None):
            prompt = self._build_llm_prompt(report_date, payload, template, references)
            system_prompt = (
                "你是董事长工作日报写作助手。"
                "请把零散工作要点整理成正式日报。"
                "要求：中文、克制、直接、避免空话，尽量保留事实、数据、判断和下一步动作。"
            )
            self.log("正在调用 LLM 生成日报初稿...")
            result, error = self.llm.call(system_prompt, prompt)
            if result:
                return result.strip()
            self.log(f"LLM 调用失败，改用本地模板兜底：{error}")

        return self._build_fallback_markdown(report_date, payload)

    def _build_llm_prompt(self, report_date, payload, template, references):
        subject = (payload.get("subject") or "").strip()
        work_items = self._normalize_block(payload.get("work_items"))
        metrics = self._normalize_block(payload.get("metrics"))
        tech_thoughts = self._normalize_block(payload.get("tech_thoughts"))
        issues = self._normalize_block(payload.get("issues"))
        next_steps = self._normalize_block(payload.get("next_steps"))
        extra = self._normalize_block(payload.get("extra_requirements"))

        reference_blocks = []
        for item in references:
            reference_blocks.append(
                f"参考日报：{os.path.basename(item['path'])}\n"
                f"{item['content'][:2200].strip()}"
            )
        reference_text = "\n\n".join(reference_blocks) if reference_blocks else "（无可用历史日报）"

        date_label = f"{report_date.year}.{report_date.month}.{report_date.day}"
        return f"""
请根据下面的信息，生成一篇发给董事长的工作日报初稿。

写作目标：
1. 让董事长快速看懂今天做了什么、为什么重要、判断是什么、接下来怎么推进。
2. 语言不要像技术文档，也不要写成流水账。
3. 尽量吸收参考日报的表达习惯，但不要机械照抄。
4. 如果我提供了数据、结论、风险，请优先写进去。
5. 结构固定为 Markdown，严格输出下面格式，不要额外解释：

# 工作日报-张丕哲-{date_label}

## 主题标题

### 背景/起因
### 现状分析
### 问题/挑战
### 分析与思考
### 方案/建议
### 下一步

补充要求：
- 主题标题如果我没写，请你自己概括，控制在 8-18 个字。
- 若某个部分信息不足，不要空着，做克制补全，但不要编造数据。
- 要体现产品需求分析、运营数据分析、新技术思考或应用中的至少一项；如果我给了多项，就自然融合。
- 不要写“赋能、抓手、闭环、颗粒度、链路”等过度包装词。

今日输入：
主题标题：{subject or '（未提供，请自动概括）'}

今天做的事项：
{work_items or '（未提供）'}

关键数据/事实：
{metrics or '（未提供）'}

新技术思考/应用：
{tech_thoughts or '（未提供）'}

问题/风险：
{issues or '（未提供）'}

明日计划：
{next_steps or '（未提供）'}

额外要求：
{extra or '（无）'}

固定模板（如果有价值可参考）：
{template or '（无）'}

最近历史日报参考：
{reference_text}
""".strip()

    def _build_fallback_markdown(self, report_date, payload):
        subject = (payload.get("subject") or "").strip() or self._guess_subject(payload)
        work_items = self._normalize_block(payload.get("work_items"))
        metrics = self._normalize_block(payload.get("metrics"))
        tech_thoughts = self._normalize_block(payload.get("tech_thoughts"))
        issues = self._normalize_block(payload.get("issues"))
        next_steps = self._normalize_block(payload.get("next_steps"))

        date_label = f"{report_date.year}.{report_date.month}.{report_date.day}"
        background = self._join_parts(
            [
                f"今日重点围绕“{subject}”推进。",
                self._first_sentence(work_items),
            ]
        )
        current_state = self._join_parts([work_items, metrics], default="今天已完成相关信息收集和问题梳理。")
        problems = issues or "当前主要挑战仍集中在需求细化、数据验证和推进节奏把控上。"
        thinking = self._join_parts(
            [
                tech_thoughts,
                "整体判断是，这项工作适合继续用产品分析和数据验证结合的方式推进，先把关键事实看清，再决定落地路径。",
            ]
        )
        suggestion = self._join_parts(
            [
                "建议继续聚焦最关键的一两个场景，把可验证的数据、需求边界和实现方式先固化下来。",
                tech_thoughts if tech_thoughts else "",
            ]
        )
        next_action = next_steps or "下一步会继续补充事实依据，明确优先级，并形成可执行的推进方案。"

        return "\n\n".join(
            [
                f"# 工作日报-张丕哲-{date_label}",
                f"## {subject}",
                f"### 背景/起因\n{background}",
                f"### 现状分析\n{current_state}",
                f"### 问题/挑战\n{problems}",
                f"### 分析与思考\n{thinking}",
                f"### 方案/建议\n{suggestion}",
                f"### 下一步\n{next_action}",
                "---\n*本初稿由 AI 辅助生成，请根据实际情况修改润色*",
            ]
        ).strip()

    def _load_template(self, template_path):
        if not template_path:
            return ""
        real_path = os.path.abspath(os.path.expanduser(template_path))
        if not os.path.exists(real_path):
            return ""
        with open(real_path, "r", encoding="utf-8") as file:
            return file.read().strip()

    def _load_history_samples(self, history_dir, report_date, limit):
        if not os.path.isdir(history_dir):
            return []

        candidates = []
        for name in os.listdir(history_dir):
            if not name.startswith("工作日报-张丕哲-"):
                continue
            lower_name = name.lower()
            if not (lower_name.endswith(".docx") or lower_name.endswith(".md")):
                continue
            if lower_name.endswith(".docx.md"):
                continue
            file_date = self._extract_date_from_name(name)
            if not file_date or file_date >= report_date:
                continue
            candidates.append((file_date, os.path.join(history_dir, name)))

        samples = []
        for _, path in sorted(candidates, key=lambda item: item[0], reverse=True):
            try:
                content = self._read_document(path)
            except Exception as exc:
                self.log(f"跳过无法读取的历史日报 {path}: {exc}")
                continue

            content = self._clean_text(content)
            if not content:
                continue

            samples.append({"path": path, "content": content})
            if len(samples) >= limit:
                break

        return samples

    def _read_document(self, path):
        lower_path = path.lower()
        if lower_path.endswith(".md"):
            with open(path, "r", encoding="utf-8") as file:
                return file.read()
        if lower_path.endswith(".docx"):
            return self._read_docx(path)
        raise ValueError(f"暂不支持的文件格式: {path}")

    def _read_docx(self, path):
        namespaces = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        }
        with zipfile.ZipFile(path) as archive:
            xml_bytes = archive.read("word/document.xml")

        root = ET.fromstring(xml_bytes)
        paragraphs = []
        for paragraph in root.findall(".//w:body/w:p", namespaces):
            texts = [node.text for node in paragraph.findall(".//w:t", namespaces) if node.text]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)
        return "\n".join(paragraphs)

    def _export_docx(self, markdown_content, base_name, output_dir):
        docx_path = os.path.join(output_dir, f"{base_name}.docx")
        plain_text = self._markdown_to_plain_text(markdown_content)

        temp_dir = tempfile.mkdtemp(prefix="daily_report_")
        text_path = os.path.join(temp_dir, f"{base_name}.txt")

        try:
            with open(text_path, "w", encoding="utf-8") as file:
                file.write(plain_text)
            completed = subprocess.run(
                ["textutil", "-convert", "docx", text_path, "-output", docx_path],
                check=True,
                capture_output=True,
                text=True,
            )
            if completed.stderr.strip():
                return None, completed.stderr.strip()
            if not os.path.exists(docx_path):
                return None, "DOCX 文件未成功生成"
            return docx_path, None
        except Exception as exc:
            return None, str(exc)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _parse_report_date(self, value):
        if not value:
            return datetime.now()
        return datetime.strptime(value.strip(), "%Y-%m-%d")

    def _extract_date_from_name(self, name):
        match = re.search(r"(20\d{2})[.\-_]?(\d{1,2})[.\-_]?(\d{1,2})", name)
        if not match:
            return None
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    def _normalize_block(self, value):
        if not value:
            return ""
        return re.sub(r"\n{3,}", "\n\n", value.strip())

    def _clean_text(self, text):
        return re.sub(r"\n{3,}", "\n\n", (text or "").strip())

    def _guess_subject(self, payload):
        first_line = self._first_sentence(payload.get("work_items") or "")
        if first_line:
            return first_line[:18]
        tech_line = self._first_sentence(payload.get("tech_thoughts") or "")
        if tech_line:
            return tech_line[:18]
        return "今日重点工作推进"

    def _first_sentence(self, text):
        if not text:
            return ""
        for line in text.splitlines():
            clean_line = line.strip().lstrip("-•1234567890.、")
            if clean_line:
                return clean_line
        return ""

    def _join_parts(self, parts, default=""):
        merged = " ".join(part.strip() for part in parts if part and part.strip())
        return merged or default

    def _markdown_to_plain_text(self, content):
        lines = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("# "):
                lines.append(line[2:].strip())
            elif line.startswith("## "):
                lines.append("")
                lines.append(line[3:].strip())
            elif line.startswith("### "):
                lines.append("")
                lines.append(line[4:].strip())
            elif line == "---":
                lines.append("")
            else:
                lines.append(line.replace("*", ""))
        return "\n".join(lines).strip() + "\n"
