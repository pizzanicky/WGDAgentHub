import os
import argparse
from dotenv import load_dotenv

# 导入我们的 Provider
from core.providers.jira_provider import JiraProvider
from core.providers.llm_provider import LLMProvider
from core.providers.data_provider import DataProvider
from core.providers.email_provider import EmailProvider

# 导入具体的 Agent
from agents.project_management.workhour_fetcher import WorkhourFetcher
from agents.dev_assistant.work_estimator import WorkEstimator
from agents.automation.data_analyst import DataAnalystAgent
# 加载 .env 中的环境变量
load_dotenv()

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

def get_absolute_path(rel_path, default_rel):
    path = rel_path or default_rel
    if os.path.isabs(path):
        return path
    return os.path.join(ROOT_DIR, path)

def main():
    parser = argparse.ArgumentParser(description="WGDAgentHub: 统一智能 Agent 工具箱")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 命令 1: jira-fetch
    fetch_parser = subparsers.add_parser("jira-fetch", help="从 Jira 抓取项目工时")
    fetch_parser.add_argument("--project", required=True, help="Jira 项目 Key")

    # 命令 2: estimate
    estimate_parser = subparsers.add_parser("estimate", help="基于历史数据进行工时预估")
    group = estimate_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--story", help="用户故事字符串")
    group.add_argument("--file", help="需求文档 .md 文件路径")

    # 命令 3: data-analyze (新增)
    analyze_parser = subparsers.add_parser("data-analyze", help="执行数据分析并生成报告 (可发送邮件)")
    analyze_parser.add_argument("--file", required=True, help="数据源文件路径 (xlsx, csv)")
    analyze_parser.add_argument("--skill", required=True, help="业务 Skill 名称 (如 shop_opinion)")
    analyze_parser.add_argument("--to", help="接收报告的邮箱地址")

    # 命令 4: create-agent
    create_parser = subparsers.add_parser("create-agent", help="[脚手架] 生成新 Agent 模板")
    create_parser.add_argument("--name", required=True, help="Agent 名称")
    create_parser.add_argument("--category", default="dev_assistant", help="分类目录")

    # 命令 5: web
    web_parser = subparsers.add_parser("web", help="启动本地网页界面")
    web_parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"), help="监听地址")
    web_parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT") or 7860), help="监听端口")

    args = parser.parse_args()

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
        print("\n" + "="*50 + "\n" + result)

    elif args.command == "data-analyze":
        # 初始化需要的 Provider
        data_prov = DataProvider()
        llm_prov = LLMProvider(os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_MODEL"))
        email_prov = EmailProvider(
            os.getenv("SMTP_SERVER"), 
            int(os.getenv("SMTP_PORT") or 465), 
            os.getenv("SMTP_USER"), 
            os.getenv("SMTP_PASS")
        )
        
        agent = DataAnalystAgent(data_prov, llm_prov, email_prov)
        result = agent.run(args.file, args.skill, args.to)
        print("\n" + "="*50 + "\n分析完成，报告如下：\n" + "="*50 + "\n")
        print(result)

    elif args.command == "create-agent":
        target_dir = os.path.join(ROOT_DIR, "agents", args.category)
        os.makedirs(target_dir, exist_ok=True)
        target_file = os.path.join(target_dir, f"{args.name}.py")
        template = f"from core.base import BaseAgent\n\nclass {args.name.capitalize()}Agent(BaseAgent):\n    def __init__(self, provider=None):\n        self.provider = provider\n\n    def run(self, *args, **kwargs):\n        self.log('开始执行 {args.name}...')\n        pass\n"
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(template)
        print(f"\n[OK] 已生成新 Agent 模板: {target_file}")

    elif args.command == "web":
        from web_ui import create_app
        app = create_app()
        print(f"\n[OK] 网页界面已启动: http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=False)

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
