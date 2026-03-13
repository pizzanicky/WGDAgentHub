import os
import glob

class WorkEstimator:
    def __init__(self, llm_provider):
        self.llm = llm_provider

    def load_history(self, history_dir):
        history = ""
        for file in glob.glob(os.path.join(history_dir, "*.md")):
            with open(file, 'r', encoding='utf-8') as f:
                history += f"\n--- Reference History ({os.path.basename(file)}) ---\n{f.read()[:10000]}\n"
        return history

    def estimate(self, story_content, history_dir):
        print("[*] 正在加载历史工时数据...")
        history = self.load_history(history_dir)
        
        system_prompt = "你是一个资深的研发估算专家。请依据提供的历史项目工时数据，对新输入的 User Story 进行任务拆分和工时评估。"
        user_prompt = f"参考数据如下:\n{history}\n\n新需求文档内容:\n{story_content}\n\n要求：参照历史风格进行任务拆分，输出总工时和每个子任务的具体时间及理由。"

        print("[*] 正在调用 LLM 进行深度评估...")
        result, error = self.llm.call(system_prompt, user_prompt)
        return result or f"Error: {error}"
