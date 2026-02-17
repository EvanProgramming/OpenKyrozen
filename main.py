import ollama
import json
from memory import MemoryBank
from tools import execute_tool, AVAILABLE_TOOLS

# 初始化
memory = MemoryBank()
MODEL_NAME = "qwen3-coder:30b"  # 确保你ollama里有这个模型

def chat_with_llm(user_input, past_memories):
    """
    构造Prompt并调用Ollama。
    """
    
    # 动态生成工具列表说明
    tools_desc = "\n".join([f"- {k}" for k in AVAILABLE_TOOLS.keys()])
    
    # --- 核心 Prompt (最重要的部分) ---
    system_prompt = f"""
    你是一个拥有自主学习能力的 AI Agent。
    
    【可用工具列表】:
    {tools_desc}
    
    【你的记忆/经验】(参考这些经验来决定如何行动):
    {past_memories}
    
    【指令】:
    1. 如果用户问题简单，直接回答。
    2. 如果需要使用工具，请**务必**只输出一个 JSON 对象，格式如下：
       {{ "action": "工具名", "args": "参数字符串" }}
    3. 不要输出 markdown 代码块（如 ```json），直接输出 JSON 字符串。
    """

    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_input}
    ]

    print("   [思考中] ...")
    response = ollama.chat(model=MODEL_NAME, messages=messages)
    return response['message']['content']

def main():
    print(f"🤖 Agent 已启动 ({MODEL_NAME})。输入 'exit' 退出。")
    
    while True:
        user_input = input("\n你: ")
        if user_input.lower() == "exit":
            break

        # --- 步骤 1: 回忆 (Recall) ---
        # 在回答之前，先去脑子里搜一下：我以前遇到过类似问题吗？怎么解决的？
        relevant_memory = memory.recall(user_input)
        context_str = "\n".join(relevant_memory) if relevant_memory else "暂无相关记忆。"
        
        # --- 步骤 2: 思考 (Think) ---
        response = chat_with_llm(user_input, context_str)
        
        # --- 步骤 3: 解析与行动 (Parse & Act) ---
        # 我们尝试解析模型是不是输出了 JSON
        try:
            # 清理一下字符串，防止模型输出前后有空格
            cleaned_response = response.strip()
            
            # 简单的启发式判断：看起来像 JSON 吗？
            if "{" in cleaned_response and "}" in cleaned_response:
                # 提取 JSON 部分
                start = cleaned_response.find("{")
                end = cleaned_response.rfind("}") + 1
                json_str = cleaned_response[start:end]
                
                command = json.loads(json_str)
                action = command.get("action")
                args = command.get("args")
                
                print(f"   [检测到动作] 工具: {action} | 参数: {args}")
                
                # 执行工具
                result = execute_tool(action, args)
                print(f"   [工具输出] {result}")
                
                # --- 步骤 4: 进化 (Learn) ---
                # 如果工具执行成功（没有返回错误），我们就把这次经验存起来！
                # 下次遇到类似问题，步骤1就能查到这个记录。
                if "错误" not in result and "失败" not in result:
                    learning_log = f"用户问题: '{user_input}' -> 我使用了工具 '{action}' 参数 '{args}' -> 结果成功。"
                    memory.add_log(learning_log)
                
                print(f"Agent: 任务已完成。执行结果: {result}")
                
            else:
                # 只是普通对话
                print(f"Agent: {response}")

        except json.JSONDecodeError:
            # 模型尝试输出 JSON 但格式错了，或者只是普通文本包含大括号
            print(f"Agent: {response}")
        except Exception as e:
            print(f"   [系统错误] {e}")

if __name__ == "__main__":
    main()