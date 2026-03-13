import os

class WorkhourFetcher:
    def __init__(self, jira_provider):
        self.jira = jira_provider

    def format_duration(self, seconds):
        if not seconds: return "0h"
        hours, remainder = divmod(seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    def run(self, project_key, output_dir):
        print(f"[*] 分析项目 {project_key} 的结构...")
        issues, error = self.jira.search_issues(f"project = '{project_key}'", 
                                                "summary,description,timeoriginalestimate,issuetype,parent,issuelinks")
        if error: return f"Error: {error}"

        stories = {}
        standalone = []

        # 1. 过滤故事
        for issue in issues:
            fields = issue.get("fields", {})
            itype = fields.get("issuetype", {}).get("name")
            if itype == "故事":
                stories[issue["key"]] = {
                    "summary": fields.get("summary"),
                    "description": fields.get("description"),
                    "tasks": [],
                    "total_estimate": 0
                }

        # 2. 分配任务
        for issue in issues:
            fields = issue.get("fields", {})
            itype = fields.get("issuetype", {}).get("name")
            if itype in ["任务", "子任务"]:
                estimate = fields.get("timeoriginalestimate") or 0
                parent = fields.get("parent", {}).get("key")
                
                task_info = {
                    "key": issue["key"],
                    "summary": fields.get("summary"),
                    "estimate": estimate,
                    "estimate_formatted": self.format_duration(estimate)
                }

                if parent in stories:
                    stories[parent]["tasks"].append(task_info)
                    stories[parent]["total_estimate"] += estimate
                else:
                    # 检查链接
                    linked = False
                    for link in fields.get("issuelinks", []):
                        target = (link.get("outwardIssue") or link.get("inwardIssue") or {}).get("key")
                        if target in stories:
                            stories[target]["tasks"].append(task_info)
                            stories[target]["total_estimate"] += estimate
                            linked = True; break
                    if not linked and estimate > 0:
                        standalone.append(task_info)

        # 3. 输出报表 (简化展示，保持 AI 可读)
        output_file = os.path.join(output_dir, f"{project_key}_story_summary.md")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"# Jira Project Report: {project_key}\n\n")
            for key, s in stories.items():
                if s['total_estimate'] > 0:
                    f.write(f"### Story: [{key}] {s['summary']}\n")
                    f.write(f"- Total Estimate: {self.format_duration(s['total_estimate'])}\n")
                    for t in s['tasks']:
                        f.write(f"  - [{t['key']}] {t['summary']} ({t['estimate_formatted']})\n")
                    f.write("\n")
        
        return f"Successfully saved to {output_file}"
