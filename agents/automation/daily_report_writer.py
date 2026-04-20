import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests

from core.base import BaseAgent


class DailyReportWriter(BaseAgent):
    DEFAULT_TOPIC_COUNT = 5
    DEFAULT_NOVELTY_WINDOW = 10
    LANE_LABELS = {
        "today": "当天工作",
        "history": "历史延展",
        "external": "外部信息",
        "hybrid": "混合选题",
    }

    def __init__(self, llm_provider=None):
        self.llm = llm_provider

    def run(self, payload):
        mode = (payload.get("mode") or "generate_report").strip()
        if mode == "suggest_topics":
            return self.suggest_topics(payload)
        if mode == "generate_report":
            return self.generate_report(payload)
        raise ValueError(f"不支持的日报模式: {mode}")

    def suggest_topics(self, payload):
        report_date = self._parse_report_date(payload.get("date"))
        history_dir = os.path.abspath(os.path.expanduser(payload["history_dir"]))
        output_dir = os.path.abspath(os.path.expanduser(payload["output_dir"]))
        os.makedirs(output_dir, exist_ok=True)

        count = max(1, int(payload.get("count") or self.DEFAULT_TOPIC_COUNT))
        novelty_window = max(1, int(payload.get("novelty_window") or self.DEFAULT_NOVELTY_WINDOW))
        reference_limit = max(1, int(payload.get("reference_limit") or 5))

        references = self._load_history_samples(
            history_dir,
            report_date,
            max(reference_limit, novelty_window, 12),
        )
        recent_topics = self._extract_recent_topics(references, novelty_window)
        external_signals, external_logs = self._load_external_signals(payload.get("source_config"))
        seed_topics = self._build_seed_topics(payload, recent_topics, external_signals, count)

        topic_suggestions = seed_topics
        if self._llm_enabled():
            llm_topics = self._refine_topics_with_llm(
                report_date,
                payload,
                seed_topics,
                recent_topics,
                external_signals,
                count,
            )
            if llm_topics:
                topic_suggestions = llm_topics

        topic_suggestions = self._finalize_topics(topic_suggestions, recent_topics, count)
        json_path, markdown_path = self._save_topic_outputs(report_date, output_dir, topic_suggestions)

        return {
            "topic_suggestions": topic_suggestions,
            "topic_suggestions_json_path": json_path,
            "topic_suggestions_markdown_path": markdown_path,
            "external_logs": external_logs,
            "references": references[:reference_limit],
            "used_llm": self._llm_enabled(),
        }

    def generate_report(self, payload):
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
        selected_topic = self._resolve_selected_topic(payload)

        content = self._generate_content(report_date, payload, template, references, selected_topic)
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
            "selected_topic": selected_topic,
            "used_llm": self._llm_enabled(),
        }

    def _generate_content(self, report_date, payload, template, references, selected_topic):
        if self._llm_enabled():
            prompt = self._build_llm_prompt(report_date, payload, template, references, selected_topic)
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

        return self._build_fallback_markdown(report_date, payload, selected_topic)

    def _build_llm_prompt(self, report_date, payload, template, references, selected_topic):
        subject = (payload.get("subject") or "").strip() or selected_topic["title"]
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

        topic_packet = json.dumps(
            {
                "title": selected_topic["title"],
                "lane": selected_topic["lane_label"],
                "why_this_topic": selected_topic["why_this_topic"],
                "source_summary": selected_topic["source_summary"],
                "today_linkage": selected_topic["today_linkage"],
                "novelty_note": selected_topic["novelty_note"],
            },
            ensure_ascii=False,
            indent=2,
        )

        date_label = f"{report_date.year}.{report_date.month}.{report_date.day}"
        return f"""
请根据下面的信息，生成一篇发给董事长的工作日报初稿。

写作目标：
1. 让董事长快速看懂今天做了什么、为什么重要、判断是什么、接下来怎么推进。
2. 语言不要像技术文档，也不要写成流水账。
3. 已经选定了今日题目，正文必须围绕这个题目展开，不能偏题。
4. 如果题目来自外部信息，但和今天工作关联不强，允许写成独立专题分析；但不要伪装成“今天已经完成落地”。
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
- 主题标题优先采用已选题目，可按需要微调，但不要偏离原意。
- 若某个部分信息不足，不要空着，做克制补全，但不要编造数据。
- 要体现产品需求分析、运营数据分析、新技术思考或应用中的至少一项；如果我给了多项，就自然融合。
- 不要写“赋能、抓手、闭环、颗粒度、链路”等过度包装词。

已选题目包：
{topic_packet}

今日输入：
主题标题：{subject}

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

    def _build_fallback_markdown(self, report_date, payload, selected_topic):
        subject = (payload.get("subject") or "").strip() or selected_topic["title"]
        work_items = self._normalize_block(payload.get("work_items"))
        metrics = self._normalize_block(payload.get("metrics"))
        tech_thoughts = self._normalize_block(payload.get("tech_thoughts"))
        issues = self._normalize_block(payload.get("issues"))
        next_steps = self._normalize_block(payload.get("next_steps"))

        date_label = f"{report_date.year}.{report_date.month}.{report_date.day}"
        background = self._join_parts(
            [
                f"今日重点围绕“{subject}”展开。",
                selected_topic["why_this_topic"],
                self._first_sentence(work_items),
            ],
            default="今日围绕重点议题进行了梳理和判断。",
        )
        current_state = self._join_parts(
            [
                work_items,
                metrics,
                f"选题来源：{selected_topic['source_summary']}",
            ],
            default="今天主要完成了相关材料收集和问题梳理。",
        )
        problems = self._join_parts(
            [
                issues,
                selected_topic["novelty_note"],
            ],
            default="当前仍需在事实验证、优先级判断和落地路径之间做好取舍。",
        )
        thinking = self._join_parts(
            [
                tech_thoughts,
                f"与今天工作的关联：{selected_topic['today_linkage']}",
                "整体判断是，先把关键事实和业务价值说明白，再决定是否进入更深的产品化或运营化推进。",
            ]
        )
        suggestion = self._join_parts(
            [
                "建议继续围绕最有经营价值、最能体现判断力的角度展开，避免把日报写成普通流水账。",
                selected_topic["source_summary"],
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

    def _build_seed_topics(self, payload, recent_topics, external_signals, count):
        subject = (payload.get("subject") or "").strip()
        work_lines = self._extract_lines(payload.get("work_items"))
        metric_lines = self._extract_lines(payload.get("metrics"))
        tech_lines = self._extract_lines(payload.get("tech_thoughts"))
        issue_lines = self._extract_lines(payload.get("issues"))

        today_topics = []
        if subject:
            today_topics.append(
                self._make_topic(
                    title=subject,
                    lane="today",
                    why_this_topic="这是你主动提供的主题，本身就代表今天最希望向董事长汇报的重点。",
                    source_summary="来源：你今天手动指定的主题。",
                    today_linkage="强：可直接和当天工作内容衔接。",
                )
            )

        for line in work_lines[:3]:
            today_topics.append(
                self._make_topic(
                    title=self._to_topic_title(line, "工作推进"),
                    lane="today",
                    why_this_topic="今天已经有实际动作，适合直接转成可汇报的重点议题。",
                    source_summary=f"来源：今天工作事项“{line[:36]}”。",
                    today_linkage="强：题目直接来自当天工作。",
                )
            )

        if metric_lines:
            metric_topic = metric_lines[0]
            today_topics.append(
                self._make_topic(
                    title=self._to_topic_title(metric_topic, "数据复盘"),
                    lane="today",
                    why_this_topic="题目能自然带出数据、判断和经营启发，较容易写出分量。",
                    source_summary=f"来源：今天提供的数据/事实“{metric_topic[:36]}”。",
                    today_linkage="强：有数据支撑，容易成稿。",
                )
            )

        if tech_lines:
            tech_topic = tech_lines[0]
            today_topics.append(
                self._make_topic(
                    title=self._to_topic_title(tech_topic, "技术判断"),
                    lane="today",
                    why_this_topic="技术思考类内容能体现部门在数字化和 AI 应用上的判断力。",
                    source_summary=f"来源：今天的技术思考“{tech_topic[:36]}”。",
                    today_linkage="中强：需结合今天工作展开。",
                )
            )

        history_topics = []
        for item in recent_topics[:4]:
            history_topics.append(
                self._make_topic(
                    title=self._extend_history_title(item["title"]),
                    lane="history",
                    why_this_topic="这个方向在历史日报中已出现过，继续深挖更稳，也更容易写出连续判断。",
                    source_summary=f"来源：历史日报《{item['title']}》。",
                    today_linkage="中：可作为今天工作的延展或补题。",
                )
            )

        external_topics = []
        for signal in external_signals[:3]:
            external_topics.append(
                self._make_topic(
                    title=self._build_external_title(signal),
                    lane="external",
                    why_this_topic="这是最近外部公开信息中值得关注的信号，适合作为补题或专题分析。",
                    source_summary=f"来源：{signal['source_name']}，{signal['summary']}",
                    today_linkage="弱到中：优先结合当天工作，必要时可独立成稿。",
                )
            )

        hybrid_topics = []
        focus_text = self._first_non_empty([subject] + work_lines + tech_lines + metric_lines + issue_lines)
        if external_signals:
            anchor_signal = external_signals[0]
            hybrid_title = self._build_hybrid_title(anchor_signal, focus_text)
            hybrid_topics.append(
                self._make_topic(
                    title=hybrid_title,
                    lane="hybrid",
                    why_this_topic="这个题目把外部变化和你当前职责结合起来，更容易体现判断和前瞻性。",
                    source_summary=f"来源：{anchor_signal['source_name']}，并结合今天关注点“{focus_text[:24] if focus_text else '商管数字化与 AI'}”。",
                    today_linkage="中强：适合把外部信息和当天工作挂钩。",
                )
            )

        ordered = []
        ordered.extend(today_topics[:2])
        ordered.extend(external_topics[:2])
        ordered.extend(hybrid_topics[:1])

        if len(ordered) < count:
            for bucket in [today_topics[2:], history_topics, hybrid_topics[1:], external_topics[2:]]:
                for topic in bucket:
                    ordered.append(topic)
                    if len(ordered) >= count:
                        break
                if len(ordered) >= count:
                    break

        if len(ordered) < count:
            ordered.extend(self._build_generic_topics(count - len(ordered)))

        return ordered[:count]

    def _refine_topics_with_llm(self, report_date, payload, seed_topics, recent_topics, external_signals, count):
        prompt = self._build_topic_prompt(report_date, payload, seed_topics, recent_topics, external_signals, count)
        system_prompt = (
            "你是董事长工作日报选题助手。"
            "你的任务不是写正文，而是挑出今天最值得写的题。"
            "请输出严格 JSON。"
        )
        self.log("正在调用 LLM 生成候选题...")
        result, error = self.llm.call(system_prompt, prompt)
        if not result:
            self.log(f"LLM 选题失败，改用本地候选：{error}")
            return None

        data = self._parse_json_block(result)
        if isinstance(data, dict):
            data = data.get("topics")
        if not isinstance(data, list):
            self.log("LLM 返回的候选题不是合法列表，改用本地候选。")
            return None
        return data

    def _build_topic_prompt(self, report_date, payload, seed_topics, recent_topics, external_signals, count):
        current_input = {
            "date": report_date.strftime("%Y-%m-%d"),
            "subject": payload.get("subject") or "",
            "work_items": self._normalize_block(payload.get("work_items")),
            "metrics": self._normalize_block(payload.get("metrics")),
            "tech_thoughts": self._normalize_block(payload.get("tech_thoughts")),
            "issues": self._normalize_block(payload.get("issues")),
            "next_steps": self._normalize_block(payload.get("next_steps")),
        }
        recent_titles = [item["title"] for item in recent_topics[:10]]
        external_brief = [
            {
                "source_name": item["source_name"],
                "title": item["title"],
                "summary": item["summary"],
                "published_at": item["published_at"],
            }
            for item in external_signals[:6]
        ]

        return f"""
请基于下面信息，为今天的董事长日报输出 {count} 个候选题。

目标：
1. 优先判断当天工作是否足够直接成题。
2. 如果当天内容较薄，就用历史日报延展和外部新信息补题。
3. 最终仍要同时保留内部题和外部题，不要全都来自同一路径。
4. 候选题必须具体、像日报标题，不能写成空泛口号。
5. 近10篇类似题目要尽量避开，但不是完全禁止。

请严格输出 JSON 数组，每个对象必须包含这些字段：
- title
- lane（只能是 today / history / external / hybrid）
- why_this_topic
- source_summary
- today_linkage

当前输入：
{json.dumps(current_input, ensure_ascii=False, indent=2)}

近10篇历史题目：
{json.dumps(recent_titles, ensure_ascii=False, indent=2)}

外部信息摘要：
{json.dumps(external_brief, ensure_ascii=False, indent=2)}

本地候选草稿：
{json.dumps(seed_topics[:count], ensure_ascii=False, indent=2)}
""".strip()

    def _finalize_topics(self, topics, recent_topics, count):
        recent_titles = [item["title"] for item in recent_topics]
        finalized = []
        seen_titles = set()

        for topic in topics:
            normalized = self._normalize_topic_entry(topic)
            title_key = self._normalize_title(normalized["title"])
            if not title_key or title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            normalized["novelty_note"] = self._build_novelty_note(normalized["title"], recent_titles)
            normalized["priority"] = self._score_topic(normalized, recent_titles)
            normalized["lane_label"] = self.LANE_LABELS.get(normalized["lane"], normalized["lane"])
            normalized["id"] = normalized.get("id") or self._topic_id(normalized["title"], normalized["lane"])
            finalized.append(normalized)

        if len(finalized) < count:
            for topic in self._build_generic_topics(count - len(finalized)):
                normalized = self._normalize_topic_entry(topic)
                normalized["novelty_note"] = self._build_novelty_note(normalized["title"], recent_titles)
                normalized["priority"] = self._score_topic(normalized, recent_titles)
                normalized["lane_label"] = self.LANE_LABELS.get(normalized["lane"], normalized["lane"])
                normalized["id"] = self._topic_id(normalized["title"], normalized["lane"])
                finalized.append(normalized)

        finalized.sort(key=lambda item: item["priority"], reverse=True)
        return finalized[:count]

    def _save_topic_outputs(self, report_date, output_dir, topic_suggestions):
        date_label = f"{report_date.year}.{report_date.month}.{report_date.day}"
        json_path = os.path.join(output_dir, f"日报选题建议-{date_label}.json")
        markdown_path = os.path.join(output_dir, f"日报选题建议-{date_label}.md")

        with open(json_path, "w", encoding="utf-8") as file:
            json.dump(topic_suggestions, file, ensure_ascii=False, indent=2)

        lines = [f"# 日报选题建议-{date_label}", ""]
        for index, item in enumerate(topic_suggestions, start=1):
            lines.extend(
                [
                    f"## {index}. {item['title']}",
                    f"- 类型：{item['lane_label']}",
                    f"- 推荐理由：{item['why_this_topic']}",
                    f"- 来源摘要：{item['source_summary']}",
                    f"- 与今天工作的关联：{item['today_linkage']}",
                    f"- 重复提醒：{item['novelty_note']}",
                    "",
                ]
            )

        with open(markdown_path, "w", encoding="utf-8") as file:
            file.write("\n".join(lines).strip() + "\n")

        return json_path, markdown_path

    def _load_external_signals(self, source_config_path):
        logs = []
        signals = []
        for source in self._load_sources(source_config_path):
            try:
                if source.get("parse_mode") == "article":
                    items = self._fetch_article_signal(source)
                else:
                    items = self._fetch_title_list_signals(source)
                signals.extend(items)
            except Exception as exc:
                logs.append(f"{source.get('name', source.get('url', 'unknown'))}: {exc}")
        return signals, logs

    def _load_sources(self, source_config_path):
        if source_config_path:
            real_path = os.path.abspath(os.path.expanduser(source_config_path))
            if os.path.exists(real_path):
                with open(real_path, "r", encoding="utf-8") as file:
                    data = json.load(file)
                sources = data.get("sources", [])
                if sources:
                    return [source for source in sources if source.get("enabled", True)]
        return [
            {
                "name": "商务部新闻发布",
                "url": "https://www.mofcom.gov.cn/xwfb/index.html",
                "category": "consumption_macro",
                "parse_mode": "html_title_list",
                "keywords": ["消费", "电子商务", "零售", "市场", "数字", "智能"],
                "limit": 3,
            },
            {
                "name": "国家统计局消费数据",
                "url": "https://www.stats.gov.cn/sj/zxfb/202604/t20260416_1963325.html",
                "category": "consumption_macro",
                "parse_mode": "article",
            },
            {
                "name": "OpenAI Model Release Notes",
                "url": "https://help.openai.com/en/articles/9624314-model-release-notes",
                "category": "ai_product_updates",
                "parse_mode": "article",
            },
        ]

    def _fetch_title_list_signals(self, source):
        response = self._fetch_url(source["url"])
        html = response.text
        anchors = re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.I | re.S)
        keywords = source.get("keywords") or []
        limit = int(source.get("limit") or 3)
        items = []

        for href, raw_text in anchors:
            title = self._clean_html_text(raw_text)
            if not self._looks_like_signal_title(title):
                continue
            if keywords and not any(keyword in title for keyword in keywords):
                continue
            items.append(
                {
                    "source_name": source["name"],
                    "title": title,
                    "summary": self._summarize_title(title, source.get("category")),
                    "url": urljoin(source["url"], href),
                    "category": source.get("category", "external"),
                    "published_at": self._extract_date_from_text(title) or "",
                }
            )
            if len(items) >= limit:
                break
        return items

    def _fetch_article_signal(self, source):
        response = self._fetch_url(source["url"])
        html = response.text
        title = self._extract_article_title(html) or source.get("name") or source["url"]
        paragraphs = self._extract_article_paragraphs(html)
        summary = " ".join(paragraphs[:2]).strip()
        if len(summary) > 120:
            summary = summary[:117] + "..."
        return [
            {
                "source_name": source["name"],
                "title": title,
                "summary": summary or self._summarize_title(title, source.get("category")),
                "url": source["url"],
                "category": source.get("category", "external"),
                "published_at": self._extract_date_from_text(html) or "",
            }
        ]

    def _fetch_url(self, url):
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) WGDAgentHub/1.0",
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response

    def _resolve_selected_topic(self, payload):
        selected_topic_context = payload.get("selected_topic_context")
        if isinstance(selected_topic_context, dict):
            topic = selected_topic_context
        elif selected_topic_context:
            topic = self._parse_json_block(selected_topic_context)
        else:
            topic = None

        if not isinstance(topic, dict):
            title = (payload.get("selected_topic_title") or "").strip()
            lane = (payload.get("selected_topic_lane") or "today").strip() or "today"
            if title:
                topic = self._make_topic(
                    title=title,
                    lane=lane,
                    why_this_topic="该题目由用户手动确认，用作正文生成的中心主题。",
                    source_summary="来源：用户确认的选题。",
                    today_linkage="中强：可结合当天输入继续展开。",
                )
            else:
                fallback_title = (payload.get("subject") or "").strip() or self._guess_subject(payload)
                topic = self._make_topic(
                    title=fallback_title,
                    lane="today",
                    why_this_topic="未单独选择题目，系统按当前输入自动确定中心主题。",
                    source_summary="来源：当前日报输入。",
                    today_linkage="强：直接来自当前输入。",
                )

        normalized = self._normalize_topic_entry(topic)
        normalized["lane_label"] = self.LANE_LABELS.get(normalized["lane"], normalized["lane"])
        normalized["novelty_note"] = normalized.get("novelty_note") or "本次已按所选题目生成正文。"
        normalized["id"] = normalized.get("id") or self._topic_id(normalized["title"], normalized["lane"])
        return normalized

    def _make_topic(self, title, lane, why_this_topic, source_summary, today_linkage):
        return {
            "title": title,
            "lane": lane,
            "why_this_topic": why_this_topic,
            "source_summary": source_summary,
            "today_linkage": today_linkage,
        }

    def _normalize_topic_entry(self, topic):
        title = self._clean_text(str(topic.get("title") or "今日重点工作推进"))
        lane = str(topic.get("lane") or "today").strip()
        if lane not in self.LANE_LABELS:
            lane = "today"
        return {
            "id": str(topic.get("id") or ""),
            "title": title,
            "lane": lane,
            "why_this_topic": self._clean_text(str(topic.get("why_this_topic") or "这个题目适合今天的日报。")),
            "source_summary": self._clean_text(str(topic.get("source_summary") or "来源信息未补充。")),
            "today_linkage": self._clean_text(str(topic.get("today_linkage") or "中：可结合今天输入展开。")),
            "novelty_note": self._clean_text(str(topic.get("novelty_note") or "")),
            "priority": int(topic.get("priority") or 0),
        }

    def _score_topic(self, topic, recent_titles):
        lane_score = {"today": 96, "hybrid": 88, "history": 78, "external": 72}.get(topic["lane"], 70)
        linkage_score = 10 if "强" in topic["today_linkage"] else 4 if "中" in topic["today_linkage"] else 0
        repeat_penalty = 0
        for recent_title in recent_titles[:10]:
            if self._title_similarity(topic["title"], recent_title) >= 0.45:
                repeat_penalty = 18
                break
        return lane_score + linkage_score - repeat_penalty

    def _build_novelty_note(self, title, recent_titles):
        for recent_title in recent_titles[:10]:
            if self._title_similarity(title, recent_title) >= 0.45:
                return f"近10篇里有相近题目：{recent_title}"
        return "近10篇未发现直接重复题目"

    def _extract_recent_topics(self, references, limit):
        topics = []
        seen = set()
        for item in references:
            title = self._extract_history_title(item["content"])
            normalized = self._normalize_title(title)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            topics.append({"title": title, "path": item["path"]})
            if len(topics) >= limit:
                break
        return topics

    def _extract_history_title(self, content):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        for line in lines[1:5]:
            if line.startswith("## "):
                return line[3:].strip()
            if not line.startswith("工作日报-"):
                return line
        return lines[0] if lines else "历史主题"

    def _extend_history_title(self, title):
        normalized = title.strip()
        if any(keyword in normalized for keyword in ["需求", "分析", "进展", "共创", "研究"]):
            return normalized + "的进一步判断"
        return normalized + "的延展思考"

    def _build_external_title(self, signal):
        title = signal["title"]
        category = signal.get("category") or ""
        if "ai" in category.lower() or "OpenAI" in signal["source_name"]:
            return f"{self._trim_title(title, 20)}对商管 AI 应用的启发"
        return f"{self._trim_title(title, 22)}对商业运营的影响判断"

    def _build_hybrid_title(self, signal, focus_text):
        focus = self._trim_title(focus_text or "商管数字化", 14)
        signal_title = self._trim_title(signal["title"], 16)
        return f"{signal_title}与{focus}的结合机会"

    def _build_generic_topics(self, count):
        generics = [
            self._make_topic(
                title="近期重点需求的优先级再判断",
                lane="history",
                why_this_topic="即使当天内容较散，也可以围绕已有重点需求继续做判断。",
                source_summary="来源：历史日报中的连续需求主题。",
                today_linkage="中：适合补题。",
            ),
            self._make_topic(
                title="商业运营数据分析能力的下一步提升方向",
                lane="hybrid",
                why_this_topic="既贴合部门职责，也能体现经营与数字化结合的思考。",
                source_summary="来源：部门职责与历史日报共性主题。",
                today_linkage="中：可结合今天工作补充。",
            ),
            self._make_topic(
                title="AI 在商管服务场景中的近期可落地方向",
                lane="external",
                why_this_topic="AI 方向具有持续关注价值，适合作为补题。",
                source_summary="来源：外部 AI 动向与商管场景结合。",
                today_linkage="弱到中：必要时可独立成稿。",
            ),
        ]
        return generics[:count]

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

    def _extract_date_from_text(self, text):
        if not text:
            return ""
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if not match:
            return ""
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    def _normalize_block(self, value):
        if not value:
            return ""
        return re.sub(r"\n{3,}", "\n\n", value.strip())

    def _clean_text(self, text):
        return re.sub(r"\n{3,}", "\n\n", (text or "").strip())

    def _guess_subject(self, payload):
        first_line = self._first_sentence(payload.get("work_items") or "")
        if first_line:
            return self._trim_title(first_line, 18)
        tech_line = self._first_sentence(payload.get("tech_thoughts") or "")
        if tech_line:
            return self._trim_title(tech_line, 18)
        return "今日重点工作推进"

    def _first_sentence(self, text):
        if not text:
            return ""
        for line in text.splitlines():
            clean_line = line.strip().lstrip("-•1234567890.、")
            if clean_line:
                return clean_line
        return ""

    def _first_non_empty(self, values):
        for value in values:
            if value and str(value).strip():
                return str(value).strip()
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

    def _llm_enabled(self):
        return bool(self.llm and getattr(self.llm, "api_key", None))

    def _extract_lines(self, value):
        lines = []
        for line in (value or "").splitlines():
            clean = line.strip().lstrip("-•1234567890.、")
            if clean:
                lines.append(clean)
        return lines

    def _to_topic_title(self, text, suffix):
        clean = re.sub(r"[：:。；;]+$", "", text.strip())
        if len(clean) <= 22:
            return clean
        return self._trim_title(clean, 20) + suffix

    def _trim_title(self, text, limit):
        clean = re.sub(r"\s+", "", text or "")
        if len(clean) <= limit:
            return clean
        return clean[:limit]

    def _normalize_title(self, title):
        return re.sub(r"[\W_]+", "", (title or "").lower())

    def _topic_id(self, title, lane):
        digest = hashlib.md5(f"{lane}:{title}".encode("utf-8")).hexdigest()[:10]
        return f"topic-{digest}"

    def _title_similarity(self, left, right):
        left_norm = self._normalize_title(left)
        right_norm = self._normalize_title(right)
        if not left_norm or not right_norm:
            return 0.0
        left_set = self._char_ngrams(left_norm)
        right_set = self._char_ngrams(right_norm)
        if not left_set or not right_set:
            return 0.0
        return len(left_set & right_set) / len(left_set | right_set)

    def _char_ngrams(self, text):
        if len(text) <= 2:
            return set(text)
        return {text[index:index + 2] for index in range(len(text) - 1)}

    def _clean_html_text(self, html_text):
        text = re.sub(r"<[^>]+>", " ", html_text)
        text = unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _extract_article_title(self, html):
        patterns = [
            r"<h1[^>]*>(.*?)</h1>",
            r"<title[^>]*>(.*?)</title>",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.I | re.S)
            if match:
                return self._clean_html_text(match.group(1))
        return ""

    def _extract_article_paragraphs(self, html):
        paragraphs = []
        for match in re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.I | re.S):
            text = self._clean_html_text(match)
            if len(text) >= 20:
                paragraphs.append(text)
            if len(paragraphs) >= 4:
                break
        return paragraphs

    def _looks_like_signal_title(self, title):
        if len(title) < 10 or len(title) > 60:
            return False
        if title.startswith("http"):
            return False
        if any(skip in title for skip in ["首页", "更多", "点击", "专题", "图片", "视频"]):
            return False
        return True

    def _summarize_title(self, title, category):
        if category == "ai_product_updates":
            return f"近期 AI 产品或模型更新信号：{self._trim_title(title, 34)}"
        return f"近期行业或消费信息信号：{self._trim_title(title, 34)}"

    def _parse_json_block(self, text):
        if not text:
            return None
        candidates = []
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
        candidates.extend(fenced)
        candidates.append(text)
        for candidate in candidates:
            snippet = candidate.strip()
            try:
                return json.loads(snippet)
            except Exception:
                pass

            match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", snippet)
            if not match:
                continue
            try:
                return json.loads(match.group(1))
            except Exception:
                continue
        return None
