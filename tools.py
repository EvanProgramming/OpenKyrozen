import os
import subprocess
import importlib.util
import sys

# --- 1. 定义具体的工具函数 ---

def write_file(args):
    """
    功能：写文件。
    参数格式：filename|content (文件名|内容)
    """
    try:
        # 分割文件名和内容
        if "|" not in args:
            return "错误：参数格式必须是 '文件名|内容'"
        
        filename, content = args.split("|", 1)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功：文件 '{filename}' 已写入。"
    except Exception as e:
        return f"写入失败: {str(e)}"

def read_file(args):
    """
    功能：读文件。
    参数格式：filename
    """
    try:
        if os.path.exists(args):
            with open(args, "r", encoding="utf-8") as f:
                return f.read()
        return "错误：文件不存在。"
    except Exception as e:
        return f"读取失败: {str(e)}"

def run_cmd(args):
    """
    功能：执行系统命令 (慎用！但在本地玩很有趣)
    参数格式：command (如 'dir' 或 'ping baidu.com')
    """
    try:
        result = subprocess.run(args, shell=True, capture_output=True, text=True)
        return result.stdout if result.stdout else result.stderr
    except Exception as e:
        return f"命令执行失败: {str(e)}"

# --- 2. 工具注册表 ---
# 这个字典把字符串（模型输出的指令）映射到真正的函数上
AVAILABLE_TOOLS = {
    "write_file": write_file,
    "read_file": read_file,
    "run_cmd": run_cmd
}

def load_new_tool(file_path):
    """
    黑科技：动态加载一个 Python 文件作为工具。
    参数：file_path (例如 'my_tools/bitcoin.py')
    """
    try:
        # 1. 从文件路径加载模块
        module_name = os.path.basename(file_path).replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        
        # 2. 假设这个文件里有一个叫 'main' 的函数作为入口
        if hasattr(module, 'main'):
            # 3. 把它注册到我们的工具表里！
            AVAILABLE_TOOLS[module_name] = module.main
            return f"成功：新工具 '{module_name}' 已加载！现在可以使用了。"
        else:
            return "失败：新工具文件里必须包含一个叫 'main(args)' 的函数。"
            
    except Exception as e:
        return f"加载工具失败: {str(e)}"

# 别忘了把这个元工具也注册进去，让 Agent 可以调用它！
AVAILABLE_TOOLS["load_tool"] = load_new_tool

def execute_tool(tool_name, tool_args):
    """统一执行入口"""
    if tool_name in AVAILABLE_TOOLS:
        return AVAILABLE_TOOLS[tool_name](tool_args)
    return f"错误：找不到工具 '{tool_name}'"