import os
import argparse
import sys
from dotenv import load_dotenv

# 导入我们的 Provider
from core.providers.jira_provider import JiraProvider
from core.providers.llm_provider import LLMProvider

# 导入具体的 Agent
from agents.project_management.workhour_fetcher import WorkhourFetcher
from agents.dev_assistant.work_estimator import WorkEstimator
# 加载 .env 中的环境变量
load_dotenv()

# 获取项目根目录 (WGDAgentHub 所在位置)
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

def get_absolute_path(rel_path, default_rel):
    path = rel_path or default_rel
    if os.path.isabs(path):
        return path
    return os.path.join(ROOT_DIR, path)

def main():
    parser = argparse.ArgumentParser(description="WGDAgentHub: 统一智能 Agent 工具箱")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 命令 1: jira-fetch (抓取并汇总 Jira 工时)
    fetch_parser = subparsers.add_parser("jira-fetch", help="从 Jira 抓取项目的故事和工时汇总")
    fetch_parser.add_argument("--project", required=True, help="Jira 项目 Key (如 KMP)")

    # 命令 2: estimate (基于历史数据进行评估)
    estimate_parser = subparsers.add_parser("estimate", help="基于历史数据进行工时预估和任务拆分")
    group = estimate_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--story", help="用户故事字符串")
    group.add_argument("--file", help="需求文档 .md 文件路径")

    args = parser.parse_args()

    # 路径解析
    history_dir = get_absolute_path(os.getenv("HISTORY_DIR"), "data/output")
    output_dir = get_absolute_path(os.getenv("OUTPUT_DIR"), "data/output")

    if args.command == "jira-fetch":
        jira_prov = JiraProvider(os.getenv("JIRA_URL"), os.getenv("JIRA_USER"), os.getenv("JIRA_PASS"))
        fetcher = WorkhourFetcher(jira_prov)
        result = fetcher.run(args.project, output_dir)
        print(f"\n[OK] {result}")

    elif args.command == "estimate":
        llm_prov = LLMProvider(os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_MODEL"))
        estimator = WorkEstimator(llm_prov)

        content = args.story
        if args.file:
            with open(args.file, 'r', encoding='utf-8') as f:
                content = f.read()

        result = estimator.estimate(content, history_dir)
...

        print("\n" + "="*50)
        print("【评估结果】")
        print("="*50 + "\n")
        print(result)

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
