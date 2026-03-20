import os

class DataProvider:
    """
    负责读取各种数据源文件 (xlsx, csv)。
    增加鲁棒性：在 pandas 或 numpy 不可用的环境下，使用 openpyxl 降级读取 xlsx。
    """
    def read_data(self, file_path):
        if not os.path.exists(file_path):
            return None, f"文件未找到: {file_path}"
        
        ext = os.path.splitext(file_path)[1].lower()
        
        # 优先尝试使用 Pandas
        try:
            import pandas as pd
            if ext == '.xlsx':
                df = pd.read_excel(file_path)
            elif ext == '.csv':
                try:
                    df = pd.read_csv(file_path, encoding='utf-8-sig')
                except UnicodeDecodeError:
                    df = pd.read_csv(file_path, encoding='gbk')
            else:
                return None, f"不支持的文件格式: {file_path}"
            return df, None
        except (ImportError, Exception) as e:
            # 降级逻辑：如果 pandas/numpy 报错，且文件是 xlsx，改用 openpyxl
            if ext == '.xlsx':
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(file_path, data_only=True)
                    ws = wb.active
                    rows = list(ws.iter_rows(values_only=True))
                    if not rows:
                        return None, "Excel 文件内容为空"
                    
                    headers = [str(cell) if cell is not None else f"Col_{i}" for i, cell in enumerate(rows[0])]
                    data = []
                    for row in rows[1:]:
                        data.append(dict(zip(headers, row)))
                    
                    # 为了兼容后续逻辑，我们返回一个模拟 Pandas DataFrame 的轻量对象
                    class SimpleDF:
                        def __init__(self, data, columns):
                            self.data = data
                            self.columns = columns
                        def dropna(self, subset=None):
                            # 简单模拟 dropna
                            if not subset: return self
                            col = subset[0]
                            cleaned = [r for r in self.data if r.get(col) is not None]
                            return SimpleDF(cleaned, self.columns)
                        def head(self, n):
                            return SimpleDF(self.data[:n], self.columns)
                        def iterrows(self):
                            for i, row in enumerate(self.data):
                                yield i, row
                    
                    return SimpleDF(data, headers), None
                except Exception as ex:
                    return None, f"Pandas 导入失败且 Openpyxl 读取也失败: {ex}"
            return None, f"无法加载数据处理模块: {e}"
