from abc import ABC, abstractmethod

class BaseAgent(ABC):
    """
    所有 Agent 的基类。
    强制要求实现 run 方法，确保调用逻辑一致。
    """
    @abstractmethod
    def run(self, *args, **kwargs):
        """执行 Agent 的主逻辑"""
        pass

    def log(self, message):
        """统一的日志输出格式"""
        print(f"[*] [{self.__class__.__name__}] {message}")
