from core.base import BaseAgent
import os
import json
import frontmatter

class DataAnalystAgent(BaseAgent):
    """
    工业级数据分析 Agent。
    采用“元数据驱动(Metadata-Driven)”架构，解析带有 YAML 的 Markdown Skill。
    """
    def __init__(self, data_provider, llm_provider, email_provider):
        self.data_provider = data_provider
        self.llm_provider = llm_provider
        self.email_provider = email_provider

    def run(self, file_path, skill_name, to_email, **kwargs):
        """
        :param file_path: 数据源 (xlsx, csv)
        :param skill_name: 业务 Skill (不带后缀)
        :param to_email: 接收邮箱
        """
        # 1. 加载并解析 Skill 元数据
        skill_file = os.path.join(os.path.dirname(__file__), "skills", f"{skill_name}.md")
        if not os.path.exists(skill_file):
            return f"Error: 找不到业务 Skill 配置文件 {skill_file}"
        
        # 使用 frontmatter 库解析元数据和正文
        skill_data = frontmatter.load(skill_file)
        meta = skill_data.metadata
        instructions = skill_data.content
        
        self.log(f"已加载业务 Skill: {meta.get('name', skill_name)}")
        
        # 2. 读取并预处理数据
        df, err = self.data_provider.read_data(file_path)
        if err:
            return f"Error: {err}"
            
        # 3. 智能列映射与强制性校验
        required_cols = meta.get("required_columns", [])
        mapping = {}
        for req_col in required_cols:
            found_col = self._find_column(df, [req_col])
            if not found_col:
                return f"Error: 业务规则要求包含 '{req_col}' 列，但在数据源中未找到匹配项。"
            mapping[req_col] = found_col
            
        # 4. 数据提取 (优化列识别)
        max_records = meta.get("max_records", 100)
        # 优先寻找核心内容列
        content_col = self._find_column(df, ["评价内容"]) or mapping.get(required_cols[2] if len(required_cols)>2 else required_cols[0])
        link_col = self._find_column(df, ["链接"]) or mapping.get(required_cols[3] if len(required_cols)>3 else (required_cols[1] if len(required_cols)>1 else None))
        
        self.log(f"数据列识别: 内容={content_col}, 链接={link_col}")
        
        sample_df = df.dropna(subset=[content_col]).head(max_records)
        records = []
        for idx, row in sample_df.iterrows():
            records.append({
                "id": idx + 1,
                "content": str(row[content_col]), # 恢复全量内容，不再截断
                "link": str(row[link_col]) if link_col and row[link_col] is not None else "无"
            })
            
        # 5. 生成报告 (LLM 编排)
        self.log(f"正在进行全量语义分析 (样本量: {len(records)})...")
        # 零硬编码：System Prompt 仅使用 Skill 定义，User Prompt 仅提供数据
        system_prompt = instructions
        user_prompt = json.dumps(records, ensure_ascii=False)

        self.log(f"Prompt 预估长度: {len(system_prompt) + len(user_prompt)} 字符")
        report_content, err = self.llm_provider.call(system_prompt, user_prompt)
        if err:
            return f"Error: {err}"

        # 6. 发送邮件
        target_email = to_email or meta.get("email_to")
        if target_email:
            subject = meta.get("email_subject", f"数据分析报告 - {skill_name}")
            self.log(f"正在尝试发送报告: {subject} -> {repr(target_email)}")
            success, mail_err = self.email_provider.send_email(target_email, subject, report_content)
            if not success:
                self.log(f"邮件发送失败: {mail_err}")
            else:
                self.log(f"邮件已成功发送至: {target_email}")

        return report_content
    def _find_column(self, df, candidates):
        """支持同义词和模糊匹配的列查找"""
        # 针对每个 candidate，增加一些常用的同义词
        synonyms = {
            "评价内容": ["评论", "内容", "review", "content", "text"],
            "链接": ["URL", "link", "网址", "source"]
        }
        
        for cand in candidates:
            all_cands = [cand.lower()] + [s.lower() for s in synonyms.get(cand, [])]
            for col in df.columns:
                if str(col).lower() in all_cands:
                    return col
        return None
