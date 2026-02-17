import os
import subprocess
import importlib.util
import sys

# --- 1. Tool function definitions ---

def write_file(args):
    """
    Write to a file.
    Args format: filename|content
    """
    try:
        # Split filename and content
        if "|" not in args:
            return "Error: args must be 'filename|content'"
        
        filename, content = args.split("|", 1)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Success: file '{filename}' written."
    except Exception as e:
        return f"Write failed: {str(e)}"

def read_file(args):
    """
    Read a file.
    Args format: filename
    """
    try:
        if os.path.exists(args):
            with open(args, "r", encoding="utf-8") as f:
                return f.read()
        return "Error: file does not exist."
    except Exception as e:
        return f"Read failed: {str(e)}"

def run_cmd(args):
    """
    Run a system command (use with care; useful for local experimentation).
    Args format: command (e.g. 'dir' or 'ping example.com')
    """
    try:
        result = subprocess.run(args, shell=True, capture_output=True, text=True)
        return result.stdout if result.stdout else result.stderr
    except Exception as e:
        return f"Command execution failed: {str(e)}"

# --- 2. Tool registry ---
# Maps tool names (model output) to the actual functions
AVAILABLE_TOOLS = {
    "write_file": write_file,
    "read_file": read_file,
    "run_cmd": run_cmd
}

def load_new_tool(file_path):
    """
    Dynamically load a Python file as a tool.
    Args: file_path (e.g. 'my_tools/bitcoin.py')
    """
    try:
        # 1. Load module from file path
        module_name = os.path.basename(file_path).replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        
        # 2. Expect a 'main' function as the entry point
        if hasattr(module, 'main'):
            # 3. Register it in the tool table
            AVAILABLE_TOOLS[module_name] = module.main
            return f"Success: new tool '{module_name}' loaded. You can use it now."
        else:
            return "Failed: the tool file must define a 'main(args)' function."
            
    except Exception as e:
        return f"Load tool failed: {str(e)}"

# Register this meta-tool so the Agent can call it
AVAILABLE_TOOLS["load_tool"] = load_new_tool

def execute_tool(tool_name, tool_args):
    """Unified execution entry point"""
    if tool_name in AVAILABLE_TOOLS:
        return AVAILABLE_TOOLS[tool_name](tool_args)
    return f"Error: tool '{tool_name}' not found"