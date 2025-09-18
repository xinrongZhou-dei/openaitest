#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI全能教师Web系统
支持多智能体协作、上下文管理、工具调用等功能
"""

import os
import json
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional

from flask import Flask, render_template, request, jsonify, session
import requests
from werkzeug.utils import secure_filename
import openai
from agents import (
    Agent, Runner, AsyncComputer, ComputerTool, ModelSettings, 
    Button, Environment, WebSearchTool, ImageGenerationTool
)
# 语音转文字（使用 OpenAI Whisper API 作为兼容实现）
try:
    from openai import OpenAI  # 新版OpenAI SDK
    _voice_client = OpenAI()
    def _transcribe_file_local(audio_path: str) -> str:
        # 如果没有扩展名，复制一份带 .m4a 临时文件，规避服务端格式探测失败
        temp_path = None
        if not os.path.splitext(audio_path)[1]:
            temp_path = audio_path + '.m4a'
            try:
                import shutil
                shutil.copyfile(audio_path, temp_path)
                audio_path = temp_path
            except Exception:
                pass
        with open(audio_path, 'rb') as af:
            res = _voice_client.audio.transcriptions.create(model='whisper-1', file=af)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return getattr(res, 'text', '') or (res.get('text') if isinstance(res, dict) else '')
except Exception:
    # 旧版SDK或环境不兼容时，回退到 openai.Audio.transcriptions.create
    def _transcribe_file_local(audio_path: str) -> str:
        try:
            with open(audio_path, 'rb') as af:
                res = openai.Audio.transcriptions.create(model='whisper-1', file=af)  # type: ignore
            return getattr(res, 'text', '') or (res.get('text') if isinstance(res, dict) else '')
        except Exception as e:
            raise RuntimeError(f"调用语音识别失败: {e}")

# =============================================================================
# 配置和初始化
# =============================================================================

# 设置OpenAI API Key
os.environ["OPENAI_API_KEY"] = "Your openai-api-key"

# 创建Flask应用
app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# 应用配置
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {
    'pdf', 'txt', 'doc', 'docx', 'py', 'js', 'java', 'cpp', 'c', 
    'csv', 'xlsx', 'xls', 'md', 'json',
    # 音频
    'mp3', 'wav', 'm4a', 'webm', 'ogg'
}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# 确保上传目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 全局变量存储对话历史和文件信息
conversation_history: Dict[str, List[Dict[str, Any]]] = {}
uploaded_files: Dict[str, Dict[str, Any]] = {}
# MCP 注册表（持久化到本地JSON）
mcp_registry: Dict[str, Dict[str, Any]] = {}
# 使用当前文件所在目录作为基准，避免工作目录变化导致保存位置不一致
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MCP_REGISTRY_FILE = os.path.join(DATA_DIR, 'mcps.json')
CONVERSATIONS_FILE = os.path.join(DATA_DIR, 'conversations.json')

def _ensure_data_dir(): 
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

def _load_mcp_registry():
    global mcp_registry
    _ensure_data_dir()
    if os.path.exists(MCP_REGISTRY_FILE):
        try:
            with open(MCP_REGISTRY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    mcp_registry = data
        except Exception:
            mcp_registry = {}

def _save_mcp_registry():
    _ensure_data_dir()
    try:
        with open(MCP_REGISTRY_FILE, 'w', encoding='utf-8') as f:
            json.dump(mcp_registry, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存MCP注册表失败: {e}")


# ------------------ 会话持久化 ------------------
def _load_conversations():
    global conversation_history
    _ensure_data_dir()
    if os.path.exists(CONVERSATIONS_FILE):
        try:
            with open(CONVERSATIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    conversation_history = data
        except Exception:
            conversation_history = {}

def _save_conversations():
    _ensure_data_dir()
    try:
        trimmed = {}
        for conv_id, msgs in conversation_history.items():
            if isinstance(msgs, list):
                trimmed[conv_id] = msgs[-10:]
        with open(CONVERSATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存对话失败: {e}")


# =============================================================================
# AIWebApp 主类
# =============================================================================

class AIWebApp:
    """AI全能教师Web应用主类"""
    
    def __init__(self):
        """初始化AI应用"""
        self.processed_files = set()  # 跟踪已处理的文件
        self.setup_agents()
    
    def create_common_tools(self) -> List[Any]:
        """创建通用工具集"""
        tools = []
        
        # WebSearchTool - 网络搜索工具
        try:
            web_search_tool = WebSearchTool()
            tools.append(web_search_tool)
            print("✓ WebSearchTool 初始化成功")
        except Exception as e:
            print(f"WebSearchTool 初始化失败: {e}")
        
        # ImageGenerationTool - 图像生成工具
        # 根据官方文档，尝试不同的配置方式
        try:
            # 方式2：带配置初始化
            image_gen_tool = ImageGenerationTool({'type': 'image_generation'})
            tools.append(image_gen_tool)
            print("✓ ImageGenerationTool 初始化成功（带配置）")
        except Exception as e2:
            print(f"ImageGenerationTool 带配置初始化失败: {e2}")
        
        print(f"总共初始化了 {len(tools)} 个工具")
        return tools
    
    def read_file_content(self, file_path: str) -> str:
        """读取文件内容"""
        try:
            file_extension = os.path.splitext(file_path)[1].lower()
            
            if file_extension in ['.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml', '.csv']:
                # 文本文件
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read()
            elif file_extension in ['.pdf']:
                # PDF文件 - 需要安装PyPDF2
                try:
                    import PyPDF2
                    with open(file_path, 'rb') as f:
                        reader = PyPDF2.PdfReader(f)
                        text = ""
                        for page in reader.pages:
                            text += page.extract_text() + "\n"
                        return text
                except ImportError:
                    return "PDF文件读取需要安装PyPDF2库: pip install PyPDF2"
                except Exception as e:
                    return f"PDF文件读取失败: {str(e)}"
            elif file_extension in ['.docx']:
                # Word文档 - 需要安装python-docx
                try:
                    from docx import Document
                    doc = Document(file_path)
                    text = ""
                    for paragraph in doc.paragraphs:
                        text += paragraph.text + "\n"
                    return text
                except ImportError:
                    return "Word文档读取需要安装python-docx库: pip install python-docx"
                except Exception as e:
                    return f"Word文档读取失败: {str(e)}"
            else:
                return f"不支持的文件类型: {file_extension}"
                
        except Exception as e:
            return f"读取文件时出错: {str(e)}"
    
    def setup_agents(self) -> None:
        """设置智能体系统"""
        # 创建通用工具集
        self.common_tools = self.create_common_tools()
        
        # 数学教师智能体
        self.math_teacher_agent = Agent(
            name="math teacher",
            instructions="""You are a professional mathematics teacher who can answer all user inquiries about mathematical topics in very detailed and highly accurate ways.

You have access to these tools:
- WebSearchTool: Search for latest mathematical concepts, formulas, or examples
- ImageGenerationTool: Create mathematical diagrams, graphs, or visual explanations

工具使用规则：
- 当用户要求画图、绘制函数图像、几何图形、数学图表时，MUST使用ImageGenerationTool
- 当需要最新的数学公式、定理、或数学应用实例时，MUST使用WebSearchTool
- 当需要可视化数学概念、函数关系、几何图形时，MUST使用ImageGenerationTool
- 当需要搜索数学历史、数学家信息、数学应用时，主动使用WebSearchTool

IMPORTANT: Always use tools when they would enhance your answer. Visual representations are crucial for mathematical understanding.""",
            handoff_description="You are an intelligent agent specializing in mathematical knowledge.",
            tools=self.common_tools
        )
        
        # 中文教师智能体
        self.chinese_teacher_agent = Agent(
            name="chinese teacher",
            instructions="""You are a professional Chinese language teacher who can answer all user inquiries about Chinese-language topics in very detailed and highly accurate ways.

You have access to various tools to enhance your teaching:
- WebSearchTool: Search for Chinese literature, poetry, idioms, or cultural context
- ImageGenerationTool: Create visual aids for Chinese characters, calligraphy, or cultural scenes

工具使用规则：
- 当需要搜索中国文学、古诗词、成语典故、文化背景时，MUST使用WebSearchTool
- 当需要展示汉字书写、书法作品、文化场景时，MUST使用ImageGenerationTool
- 当需要查找古诗词的详细解释、作者背景、历史背景时，主动使用WebSearchTool
- 当需要可视化汉字结构、笔画顺序、文化场景时，主动使用ImageGenerationTool

Use these tools when they would help provide better explanations or more comprehensive answers.""",
            handoff_description="You are an intelligent agent specializing in Chinese language knowledge.",
            tools=self.common_tools
        )
        
        # 物理教师智能体
        self.physics_teacher_agent = Agent(
            name="physics teacher",
            instructions="""You are a professional physics teacher who can answer all user inquiries about physics in very detailed and highly accurate ways.

You have access to various tools to enhance your teaching:
- WebSearchTool: Search for latest physics research, experiments, or real-world applications
- ImageGenerationTool: Create physics diagrams, force diagrams, wave patterns, or experimental setups

工具使用规则：
- 当需要搜索最新物理研究、实验数据、物理应用实例时，MUST使用WebSearchTool
- 当需要绘制物理图表、受力图、波形图、实验装置图时，MUST使用ImageGenerationTool
- 当需要可视化物理概念、力场、电磁场、波动现象时，主动使用ImageGenerationTool
- 当需要查找物理公式、常数、实验方法时，主动使用WebSearchTool

Use these tools when they would help provide better explanations or more comprehensive answers.""",
            handoff_description="You are an intelligent agent specializing in physics",
            tools=self.common_tools
        )
        
        # 历史教师智能体
        self.history_teacher_agent = Agent(
            name="historyer",
            instructions="""You are a professional history teacher who can answer all user inquiries about history in very detailed and highly accurate ways.

You have access to various tools to enhance your teaching:
- WebSearchTool: Search for historical facts, timelines, or recent historical discoveries
- ImageGenerationTool: Create historical maps, timelines, or visual representations of historical events

工具使用规则：
- 当需要搜索历史事实、时间线、最新历史发现时，MUST使用WebSearchTool
- 当需要创建历史地图、时间线、历史事件可视化时，MUST使用ImageGenerationTool
- 当需要展示历史人物、历史建筑、历史场景时，主动使用ImageGenerationTool
- 当需要查找历史细节、历史背景、历史影响时，主动使用WebSearchTool

Use these tools when they would help provide better explanations or more comprehensive answers.""",
            handoff_description="You are an intelligent agent specializing in history",
            tools=self.common_tools
        )
        
        # 文件分析智能体 - 专门处理文件相关的问题
        self.file_analysis_agent = Agent(
            name="file analysis agent",
            instructions="""你是一个专业的文件分析智能体，专门处理用户上传的文件内容。
            tools=self.common_tools

你的主要职责：
1. 分析文件内容，提取关键信息
2. 回答关于文件内容的问题
3. 总结文件要点
4. 解释文件中的概念和内容
5. 根据文件内容提供建议或解答

你拥有各种工具来增强你的分析能力：
- WebSearchTool: 搜索相关信息来补充文件内容
- ImageGenerationTool: 创建图表、流程图来可视化文件内容

工具使用规则：
- 当文件内容涉及需要最新信息验证或补充时，MUST使用WebSearchTool
- 当需要将文件内容可视化、创建流程图、概念图时，MUST使用ImageGenerationTool
- 当文件内容涉及复杂概念需要图表说明时，主动使用ImageGenerationTool
- 当需要搜索文件内容相关的背景信息、定义、解释时，主动使用WebSearchTool

请仔细分析用户提供的文件内容，并基于文件内容回答用户的问题。如果需要更多信息，可以使用工具进行搜索。""",
            handoff_description="You are an intelligent agent specializing in file content analysis.",
            tools=self.common_tools
        )
        
        # 通用问题智能体
        self.general_agent = Agent(
            name="general question agent",
            instructions="""你是一个通用问题回答助手，专门处理以下类型的问题：
1. 对话历史相关问题（如"我问你的第一个问题是什么？"、"我们刚才聊了什么？"）
2. 系统功能相关问题
3. 时间、日期、天气、新闻等实时信息问题
4. 不符合其他专业领域的问题
5. 需要综合多个领域知识的问题

你拥有各种工具来增强你的回答能力：
- WebSearchTool: 搜索最新信息、事实或数据
- ImageGenerationTool: 创建图表、流程图或视觉说明

工具使用规则：
- 当用户询问时间、日期、天气、新闻、股票价格等实时信息时，MUST使用WebSearchTool
- 当用户询问需要视觉化展示的内容时，MUST使用ImageGenerationTool
- 当需要最新数据、事实或信息时，主动使用WebSearchTool
- 当需要创建图表、流程图、概念图时，主动使用ImageGenerationTool

请基于对话历史来回答用户的问题，如果涉及之前的内容，请准确回忆并回答。当工具能帮助提供更好的答案时，请主动使用这些工具。""",
            handoff_description="You are an intelligent agent specializing in general questions and conversation context.",
            tools=self.common_tools
        )
        
        # Triage Agent - 智能体选择器
        self.triage_agent = Agent(
            name="Triage Agent",
            instructions="""你是一个智能体选择器。根据用户问题和对话历史选择最合适的智能体：

- math_teacher_agent: 数学、方程、计算、代数、几何、图像、函数等
- chinese_teacher_agent: 中文、文学、诗歌、成语、语法等  
- physics_teacher_agent: 物理、力学、运动定律、能量等
- history_teacher_agent: 历史、历史事件、古代文明等
- file_analysis_agent: 文件分析、文件内容相关问题、用户明确要求分析文件
- general_agent: 对话历史、系统功能、综合问题、时间日期、其他不符合上述分类的问题

重要：你的回答必须只包含一个智能体名称，不要任何解释、描述或其他内容。

选择规则（按优先级）：
1. 如果用户明确要求分析文件内容（如"分析这个文件"、"总结文件内容"），选择 file_analysis_agent
2. 如果问题涉及时间、日期、当前信息、实时数据，选择 general_agent（因为它有网络搜索能力）
3. 如果问题涉及数学概念，选择 math_teacher_agent
4. 如果问题涉及中文、文学，选择 chinese_teacher_agent
5. 如果问题涉及物理概念，选择 physics_teacher_agent
6. 如果问题涉及历史，选择 history_teacher_agent
7. 其他情况选择 general_agent

特别注意：
- 语音转写后的文本内容按文本内容判断，不按文件类型判断
- 时间、日期、天气、新闻等实时信息问题选择 general_agent
- 只有明确要求分析文件内容时才选择 file_analysis_agent

例如：
- 用户问"今天的日期和现在北京的时间" → 回答：general_agent
- 用户问"画一个二次函数图像" → 回答：math_teacher_agent
- 用户问"什么是诗歌" → 回答：chinese_teacher_agent
- 用户问"分析这个PDF文件的内容" → 回答：file_analysis_agent
- 用户问"我问的第一个问题是什么" → 回答：general_agent

只返回智能体名称，不要其他任何文字。""",
            handoffs=[
                "math_teacher_agent", "chinese_teacher_agent", 
                "physics_teacher_agent", "history_teacher_agent", 
                "file_analysis_agent", "general_agent"
            ],
            tools=[]  # Triage Agent 不需要工具，只做选择
        )
    
    def build_context_prompt(self, history: List[Dict[str, Any]], current_question: str) -> str:
        """构建包含上下文的提示"""
        if not history or len(history) <= 1:
            return current_question
        
        context_parts = ["以下是我们的对话历史："]
        
        # 跟踪在当前对话中已处理的文件
        current_processed_files = set()
        
        # 只取最近8轮对话，避免token过多
        recent_history = history[-8:]
        for msg in recent_history:
            role = "用户" if msg['type'] == 'user' else "AI助手"
            message_text = msg['message']
            
            # 如果消息包含文件信息，添加文件上下文
            if msg['type'] == 'user' and 'file_id' in msg:
                file_id = msg['file_id']
                print(f"🔍 检测到文件ID: {file_id}")
                # 使用全局变量
                global uploaded_files
                print(f"📁 当前上传的文件: {list(uploaded_files.keys())}")
                # 若是语音且已经在 /api/chat 中自动转写，则直接使用转写文本，避免再次读取原音频
                if msg.get('transcribed_from_audio'):
                    file_info = uploaded_files.get(file_id, {})
                    file_name = file_info.get('name', 'audio')
                    trans_text = msg.get('transcribed_text') or message_text
                    context_parts.append(f"{role}: [上传了语音 '{file_name}' 并已自动转写]")
                    context_parts.append(f"转写文本：\n{trans_text}")
                    context_parts.append(f"{role}: {message_text}")
                elif file_id in uploaded_files and file_id not in current_processed_files:
                    file_info = uploaded_files[file_id]
                    file_path = file_info['path']
                    file_name = file_info['name']
                    print(f"📄 读取文件: {file_name}")
                    file_content = self.read_file_content(file_path)
                    print(f"📄 文件内容长度: {len(file_content)} 字符")
                    
                    # 将文件内容添加到上下文中（只添加一次）
                    context_parts.append(f"{role}: [上传了文件 '{file_name}']")
                    context_parts.append(f"文件内容：\n{file_content}")
                    current_processed_files.add(file_id)
                    context_parts.append(f"{role}: {message_text}")
                elif file_id in uploaded_files and file_id in current_processed_files:
                    # 文件在当前对话中已处理过，只添加引用
                    file_info = uploaded_files[file_id]
                    file_name = file_info['name']
                    print(f"📄 文件已处理过，只添加引用: {file_name}")
                    context_parts.append(f"{role}: [继续讨论文件 '{file_name}']")
                    context_parts.append(f"{role}: {message_text}")
                else:
                    print(f"❌ 文件ID {file_id} 未找到")
                    context_parts.append(f"{role}: {message_text}")
            else:
                context_parts.append(f"{role}: {message_text}")
        
        context_parts.extend([
            f"\n当前问题：{current_question}",
            "\n请基于以上对话历史回答当前问题。"
        ])
        
        return "\n".join(context_parts)
    
    def build_simple_context_prompt(self, history: List[Dict[str, Any]], current_question: str) -> str:
        """构建简化的上下文提示（不包含文件内容，只给 Triage Agent 用）"""
        if not history or len(history) <= 1:
            return current_question
        
        context_parts = ["以下是我们的对话历史："]
        
        # 只取最近8轮对话，避免token过多
        recent_history = history[-8:]
        for msg in recent_history:
            role = "用户" if msg['type'] == 'user' else "AI助手"
            message_text = msg['message']
            
            # 如果消息包含文件信息，只添加文件引用，不读取内容
            if msg['type'] == 'user' and 'file_id' in msg:
                file_id = msg['file_id']
                global uploaded_files
                if file_id in uploaded_files:
                    file_info = uploaded_files[file_id]
                    file_name = file_info['name']
                    context_parts.append(f"{role}: [上传了文件 '{file_name}']")
                    context_parts.append(f"{role}: {message_text}")
                else:
                    context_parts.append(f"{role}: {message_text}")
            else:
                context_parts.append(f"{role}: {message_text}")
        
        context_parts.extend([
            f"\n当前问题：{current_question}",
            "\n请基于以上对话历史回答当前问题。"
        ])
        
        return "\n".join(context_parts)
    
    def build_file_analysis_prompt(self, history: List[Dict[str, Any]], current_question: str, file_id: str = None) -> str:
        """为文件分析智能体构建包含文件内容的提示"""
        context_parts = ["以下是我们的对话历史："]
        
        # 跟踪在当前对话中已处理的文件
        current_processed_files = set()
        
        # 只取最近8轮对话，避免token过多
        recent_history = history[-8:]
        for msg in recent_history:
            role = "用户" if msg['type'] == 'user' else "AI助手"
            message_text = msg['message']
            
            # 如果消息包含文件信息，添加文件上下文
            if msg['type'] == 'user' and 'file_id' in msg:
                file_id = msg['file_id']
                print(f"🔍 文件分析智能体检测到文件ID: {file_id}")
                
                # 使用全局变量
                global uploaded_files
                if msg.get('transcribed_from_audio'):
                    file_info = uploaded_files.get(file_id, {})
                    file_name = file_info.get('name', 'audio')
                    trans_text = msg.get('transcribed_text') or message_text
                    context_parts.append(f"{role}: [上传了语音 '{file_name}' 并已自动转写]")
                    context_parts.append(f"转写文本：\n{trans_text}")
                    context_parts.append(f"{role}: {message_text}")
                elif file_id in uploaded_files and file_id not in current_processed_files:
                    file_info = uploaded_files[file_id]
                    file_path = file_info['path']
                    file_name = file_info['name']
                    print(f"📄 文件分析智能体读取文件: {file_name}")
                    file_content = self.read_file_content(file_path)
                    print(f"📄 文件内容长度: {len(file_content)} 字符")
                    
                    # 将文件内容添加到上下文中（只添加一次）
                    context_parts.append(f"{role}: [上传了文件 '{file_name}']")
                    context_parts.append(f"文件内容：\n{file_content}")
                    current_processed_files.add(file_id)
                    context_parts.append(f"{role}: {message_text}")
                elif file_id in uploaded_files and file_id in current_processed_files:
                    # 文件在当前对话中已处理过，只添加引用
                    file_info = uploaded_files[file_id]
                    file_name = file_info['name']
                    print(f"📄 文件分析智能体文件已处理过，只添加引用: {file_name}")
                    context_parts.append(f"{role}: [继续讨论文件 '{file_name}']")
                    context_parts.append(f"{role}: {message_text}")
                else:
                    print(f"❌ 文件ID {file_id} 未找到")
                    context_parts.append(f"{role}: {message_text}")
            else:
                context_parts.append(f"{role}: {message_text}")
        
        context_parts.extend([
            f"\n当前问题：{current_question}",
            "\n请基于以上对话历史和文件内容回答当前问题。"
        ])
        
        return "\n".join(context_parts)
    
    async def process_user_question(self, user_message: str, conversation_id: str, file_id: str = None) -> Dict[str, str]:
        """处理用户问题 - 中心智能体判断并调用对应专业智能体"""
        try:
            # 1. 获取对话历史
            history = conversation_history.get(conversation_id, [])
            
            # 2. 构建简化的提示给 Triage Agent（不需要文件内容）
            context_prompt = self.build_simple_context_prompt(history, user_message)
            
            # 3. 使用 Triage Agent 判断需要调用哪个智能体
            triage_result = await Runner.run(self.triage_agent, context_prompt)
            
            # 调试信息
            print(f"Triage Agent 输出: {triage_result.final_output}")
            
            # 4. 从结果中提取选择的智能体名称
            selected_agent_name = self.extract_agent_name(triage_result.final_output)
            print(f"选择的智能体: {selected_agent_name}")
            
            # 5. 构建包含上下文的提示给选中的智能体
            if selected_agent_name == "file_analysis_agent":
                # 文件分析智能体使用专门的提示构建方法
                agent_context_prompt = self.build_file_analysis_prompt(history, user_message, file_id)
            else:
                # 其他智能体使用通用提示构建方法
                agent_context_prompt = self.build_context_prompt(history, user_message)
            
            # 6. 根据选择的智能体名称调用对应的专业智能体
            agent_mapping = {
                "math_teacher_agent": (self.math_teacher_agent, "数学教师"),
                "chinese_teacher_agent": (self.chinese_teacher_agent, "中文教师"),
                "physics_teacher_agent": (self.physics_teacher_agent, "物理教师"),
                "history_teacher_agent": (self.history_teacher_agent, "历史教师"),
                "file_analysis_agent": (self.file_analysis_agent, "文件分析助手"),
                "general_agent": (self.general_agent, "通用助手")
            }
            
            if selected_agent_name in agent_mapping:
                agent, display_name = agent_mapping[selected_agent_name]
                result = await Runner.run(agent, agent_context_prompt)
                agent_display_name = display_name
            else:
                # 默认使用通用智能体
                result = await Runner.run(self.general_agent, agent_context_prompt)
                agent_display_name = "通用助手"
            
            # 7. 提取工具调用信息
            tools_used = self.extract_tools_used(result)
            
            # 8. 提取图片数据
            images = self.extract_images(result)
            
            # 9. 返回包含智能体名称、工具信息和图片的结果
            return {
                'content': result.final_output,
                'agent_name': agent_display_name,
                'agent_id': selected_agent_name,
                'tools_used': tools_used,
                'images': images
            }
            
        except Exception as e:
            return {
                'content': f"处理问题时出现错误：{str(e)}",
                'agent_name': "系统错误",
                'agent_id': "error",
                'tools_used': []
            }
    
    def extract_agent_name(self, triage_output: str) -> str:
        """从 Triage Agent 的输出中提取智能体名称"""
        output_lower = triage_output.lower()
        
        # 优先检查完整的智能体名称
        agent_names = [
            "general_agent", "chinese_teacher_agent", "physics_teacher_agent",
            "history_teacher_agent", "math_teacher_agent", "file_analysis_agent"
        ]
        
        for agent_name in agent_names:
            if agent_name in output_lower:
                return agent_name
        
        # 然后检查关键词（更精确的匹配）
        keyword_mapping = [
            ("general", "agent", "general_agent"),
            ("chinese", "teacher", "chinese_teacher_agent"),
            ("physics", "teacher", "physics_teacher_agent"),
            ("history", "teacher", "history_teacher_agent"),
            ("math", "teacher", "math_teacher_agent")
        ]
        
        for keyword1, keyword2, agent_name in keyword_mapping:
            if keyword1 in output_lower and keyword2 in output_lower:
                return agent_name
        
        # 最后检查学科关键词
        subject_mapping = [
            ("中文", "语文", "语言", "chinese_teacher_agent"),
            ("物理", "力学", "牛顿", "physics_teacher_agent"),
            ("历史", "古代", "朝代", "history_teacher_agent"),
            ("数学", "方程", "计算", "math_teacher_agent")
        ]
        
        for keywords, agent_name in subject_mapping:
            if any(keyword in output_lower for keyword in keywords):
                return agent_name
        
        # 默认返回通用智能体
        return "general_agent"
    
    def extract_tools_used(self, result) -> List[Dict[str, str]]:
        """从智能体执行结果中提取工具调用信息"""
        tools_used = []
        
        try:
            # 检查 new_items 属性中的 ToolCallItem
            if hasattr(result, 'new_items') and result.new_items:
                for item in result.new_items:
                    if 'ToolCallItem' in str(type(item)) and hasattr(item, 'raw_item'):
                        raw_item = item.raw_item
                        
                        # 从 type 属性推断工具类型
                        if hasattr(raw_item, 'type'):
                            if 'image_generation' in str(raw_item.type):
                                tool_name = 'image_generation'
                                tool_info = {
                                    'name': tool_name,
                                    'display_name': self.get_tool_display_name(tool_name),
                                    'description': self.get_tool_description(tool_name)
                                }
                                tools_used.append(tool_info)
                            elif 'web_search' in str(raw_item.type):
                                tool_name = 'web_search'
                                tool_info = {
                                    'name': tool_name,
                                    'display_name': self.get_tool_display_name(tool_name),
                                    'description': self.get_tool_description(tool_name)
                                }
                                tools_used.append(tool_info)
            
            # 显示工具使用信息
            if tools_used:
                tool_names = [tool['display_name'] for tool in tools_used]
                print(f"🔧 使用的工具: {', '.join(tool_names)}")
            
        except Exception as e:
            print(f"提取工具调用信息时出错: {e}")
        
        return tools_used
    
    def extract_images(self, result) -> List[Dict[str, str]]:
        """从智能体执行结果中提取图片数据"""
        images = []
        
        try:
            # 检查 new_items 属性中的图片生成结果
            if hasattr(result, 'new_items') and result.new_items:
                for item in result.new_items:
                    if 'ToolCallItem' in str(type(item)) and hasattr(item, 'raw_item'):
                        raw_item = item.raw_item
                        if hasattr(raw_item, 'type') and 'image_generation' in str(raw_item.type):
                            print(f"🖼️ 发现图像生成结果")
                            
                            # 检查图片数据
                            if hasattr(raw_item, 'result') and raw_item.result:
                                # 图片数据可能是URL或Base64
                                image_data = raw_item.result
                                print(f"图片数据: {type(image_data)}")
                                print(f"图片数据内容: {image_data[:200]}...")  # 显示前200个字符
                                
                                if isinstance(image_data, str):
                                    if image_data.startswith('http'):
                                        # 图片URL
                                        print(f"✓ 检测到图片URL")
                                        images.append({
                                            'type': 'url',
                                            'data': image_data,
                                            'alt': '生成的图像'
                                        })
                                    elif image_data.startswith('data:image'):
                                        # Base64图片（带data:image前缀）
                                        print(f"✓ 检测到Base64图片（带前缀）")
                                        images.append({
                                            'type': 'base64',
                                            'data': image_data,
                                            'alt': '生成的图像'
                                        })
                                    elif image_data.startswith('iVBORw0KGgo') or image_data.startswith('/9j/'):
                                        # 纯Base64图片数据（PNG或JPEG）
                                        print(f"✓ 检测到纯Base64图片数据")
                                        # 添加data:image前缀
                                        if image_data.startswith('iVBORw0KGgo'):
                                            # PNG格式
                                            base64_data = f"data:image/png;base64,{image_data}"
                                        else:
                                            # JPEG格式
                                            base64_data = f"data:image/jpeg;base64,{image_data}"
                                        
                                        images.append({
                                            'type': 'base64',
                                            'data': base64_data,
                                            'alt': '生成的图像'
                                        })
                                    else:
                                        # 可能是其他格式的字符串
                                        print(f"⚠️ 未知的图片数据格式: {image_data[:50]}...")
                                        # 尝试作为URL处理
                                        images.append({
                                            'type': 'url',
                                            'data': image_data,
                                            'alt': '生成的图像'
                                        })
                                elif isinstance(image_data, dict):
                                    # 可能是包含图片信息的字典
                                    print(f"图片数据是字典: {image_data}")
                                    if 'url' in image_data:
                                        images.append({
                                            'type': 'url',
                                            'data': image_data['url'],
                                            'alt': '生成的图像'
                                        })
                                    elif 'data' in image_data:
                                        images.append({
                                            'type': 'base64',
                                            'data': image_data['data'],
                                            'alt': '生成的图像'
                                        })
                                else:
                                    print(f"⚠️ 未知的图片数据类型: {type(image_data)}")
                            else:
                                print(f"⚠️ 没有找到图片结果数据")
                            
                            # 检查状态
                            if hasattr(raw_item, 'status'):
                                print(f"图片生成状态: {raw_item.status}")
        
        except Exception as e:
            print(f"提取图片数据时出错: {e}")
        
        return images
    
    def get_tool_display_name(self, tool_name: str) -> str:
        """获取工具的中文显示名称"""
        tool_names = {
            'web_search': '网络搜索',
            'web_search_tool': '网络搜索',
            'image_generation': '图像生成',
            'image_generation_tool': '图像生成',
            'computer_tool': '计算机工具',
            'browser_tool': '浏览器工具'
        }
        return tool_names.get(tool_name.lower(), tool_name)
    
    def get_tool_description(self, tool_name: str) -> str:
        """获取工具的描述信息"""
        tool_descriptions = {
            'web_search': '搜索网络获取最新信息',
            'web_search_tool': '搜索网络获取最新信息',
            'image_generation': '生成图像和图表',
            'image_generation_tool': '生成图像和图表',
            'computer_tool': '执行计算和模拟',
            'browser_tool': '使用浏览器自动化'
        }
        return tool_descriptions.get(tool_name.lower(), '未知工具')


# =============================================================================
# MCP 相关工具函数
# =============================================================================

def validate_mcp_connectivity(config: Dict[str, Any]) -> (bool, str):
    """校验 MCP 是否可连接，返回 (是否成功, 错误信息)"""
    try:
        if not isinstance(config, dict):
            return False, "配置需为JSON对象"
        url = config.get('url')
        if not url:
            return False, "缺少 url 字段"
        headers = config.get('headers') or {}
        timeout = float(config.get('timeout', 10))

        # 使用 GET 进行连通性探测（部分实现会要求POST，这里以GET为探测）
        resp = requests.get(url, headers=headers, timeout=timeout)
        if 200 <= resp.status_code < 300:
            return True, ""
        # 返回部分响应体帮助定位
        body = resp.text[:300] if isinstance(resp.text, str) else ''
        return False, f"HTTP {resp.status_code}: {body}"
    except requests.exceptions.RequestException as e:
        return False, f"网络异常: {str(e)}"
    except Exception as e:
        return False, f"未知错误: {str(e)}"


# =============================================================================
# 工具函数
# =============================================================================

def allowed_file(filename: str) -> bool:
    """检查文件类型是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# =============================================================================
# Flask 路由
# =============================================================================

@app.route('/')
def index():
    """主页路由 - 渲染前端页面"""
    return render_template('index.html')


@app.route('/api/chat', methods=['POST'])
def chat():
    """聊天API路由 - 处理用户消息"""
    data = request.get_json()
    user_message = data.get('message', '').strip()
    conversation_id = data.get('conversation_id')
    if not conversation_id:
        conversation_id = str(uuid.uuid4())
        print(f"🆕 创建新对话ID: {conversation_id}")
    use_web_search = data.get('use_web_search', False)
    region = data.get('region', 'auto')
    file_id = data.get('file_id')
    mode = data.get('mode', 'auto')
    
    # 调试信息
    print(f"📨 收到消息: {user_message}")
    print(f"📁 文件ID: {file_id}")
    print(f"💬 对话ID: {conversation_id}")
    
    # 允许空消息：若携带音频文件则自动生成默认问题
    # 在没有文件也没有消息的情况下仍然报错
    if not user_message:
        auto_msg_from_audio = False
        if file_id and file_id in uploaded_files:
            fpath = uploaded_files[file_id]['path']
            fext = os.path.splitext(fpath)[1].lower().strip('.')
            if fext in {'mp3','wav','m4a','webm','ogg'}:
                user_message = '请基于该语音内容给出完整摘要、要点列表与关键信息。'
                auto_msg_from_audio = True
        if not user_message:
            return jsonify({'error': '消息不能为空'}), 400
    
    # 获取或创建对话历史
    if conversation_id not in conversation_history:
        conversation_history[conversation_id] = []
    
    # 如果携带的是音频文件，则在进入对话流程前先做自动转写
    auto_transcribed = False
    if file_id and file_id in uploaded_files:
        file_info_auto = uploaded_files[file_id]
        file_path_auto = file_info_auto['path']
        name_ext = os.path.splitext(file_info_auto.get('name',''))[1].lower().strip('.')
        path_ext = os.path.splitext(file_path_auto)[1].lower().strip('.')
        ext_auto = name_ext or path_ext
        print(f"🔎 自动转写检查：ext={ext_auto}, path={file_path_auto}")
        if ext_auto in {'mp3','wav','m4a','webm','ogg'}:
            try:
                print(f"🎙️ 检测到音频文件，自动转写: {file_info_auto['name']}")
                user_message = _transcribe_file_local(file_path_auto) or user_message
                auto_transcribed = True
                print(f"📝 转写文本: {user_message[:120]}...")
            except Exception as e:
                print(f"❌ 自动转写失败: {e}")
    
    # 添加用户消息到历史（包含文件信息）
    user_message_data = {
        'timestamp': datetime.now().isoformat(),
        'type': 'user',
        'message': user_message
    }
    
    # 如果有文件，添加文件信息（同一会话内去重：已出现过的 file_id 不再重复附加）
    if file_id and file_id in uploaded_files:
        # 检查该会话历史中是否已出现过相同的 file_id
        history_has_same_file = any(
            isinstance(m, dict) and m.get('file_id') == file_id
            for m in conversation_history.get(conversation_id, [])
        )
        if not history_has_same_file:
            file_info = uploaded_files[file_id]
            user_message_data['file_id'] = file_id
            user_message_data['file_name'] = file_info['name']
            user_message_data['file_type'] = os.path.splitext(file_info['name'])[1].lower()
            print(f"✅ 文件信息已添加到消息: {file_info['name']}")
        else:
            print(f"↩️ 同会话已包含该文件ID {file_id}，跳过重复附加到消息")
    else:
        print(f"❌ 文件ID {file_id} 未找到或为空")
        print(f"📁 当前上传的文件: {list(uploaded_files.keys())}")
    
    if auto_transcribed:
        user_message_data['transcribed_from_audio'] = True
        user_message_data['transcribed_text'] = user_message
    conversation_history[conversation_id].append(user_message_data)
    # 仅保留最近10条，并持久化
    conversation_history[conversation_id] = conversation_history[conversation_id][-10:]
    _save_conversations()
    print(f"💾 消息已保存到对话历史，包含文件信息: {'file_id' in user_message_data}")
    
    # 使用中心智能体处理用户问题
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ai_result = loop.run_until_complete(ai_app.process_user_question(user_message, conversation_id, file_id))
    finally:
        loop.close()
    
    # 提取响应内容和智能体信息
    if isinstance(ai_result, dict):
        response_content = ai_result['content']
        agent_name = ai_result['agent_name']
        agent_id = ai_result['agent_id']
        tools_used = ai_result.get('tools_used', [])
        images = ai_result.get('images', [])
    else:
        # 兼容旧格式
        response_content = ai_result
        agent_name = "未知智能体"
        agent_id = "unknown"
        tools_used = []
        images = []
    
    # 添加AI回复到历史
    conversation_history[conversation_id].append({
        'timestamp': datetime.now().isoformat(),
        'type': 'assistant',
        'message': response_content,
        'agent_name': agent_name,
        'agent_id': agent_id,
        'tools_used': tools_used,
        'images': images
    })
    conversation_history[conversation_id] = conversation_history[conversation_id][-10:]
    _save_conversations()
    
    return jsonify({
        'response': response_content,
        'agent_name': agent_name,
        'agent_id': agent_id,
        'tools_used': tools_used,
        'images': images,
        'conversation_id': conversation_id
    })


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """文件上传API路由"""
    if 'file' not in request.files:
        return jsonify({'error': '没有选择文件'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '没有选择文件'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # 若 secure_filename 去掉了扩展名，则根据 mimetype 追加（音频场景尤为常见）
        base, ext = os.path.splitext(filename)
        if not ext and (file.mimetype or '').startswith('audio/'):
            mime_tail = (file.mimetype.split('/')[-1] or '').lower()
            # 规范化常见 audio/* 到文件后缀
            mime_map = {
                'x-m4a': 'm4a', 'm4a': 'm4a',
                'mpeg': 'mp3', 'mp3': 'mp3',
                'wav': 'wav', 'x-wav': 'wav', 'wave': 'wav',
                'mp4': 'mp4', 'x-mp4': 'mp4',
                'ogg': 'ogg', 'oga': 'ogg',
                'webm': 'webm'
            }
            norm = mime_map.get(mime_tail.lstrip('x-'), mime_map.get(mime_tail, 'm4a'))
            filename = base + '.' + norm
        # 添加时间戳避免文件名冲突
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # 生成文件ID
        file_id = str(uuid.uuid4())
        
        # 保存文件信息
        uploaded_files[file_id] = {
            'name': file.filename,
            'path': filepath,
            'id': file_id,
            'upload_time': datetime.now().isoformat()
        }
        
        return jsonify({
            'file_id': file_id,
            'filename': file.filename,
            'message': '文件上传成功'
        })
    
    return jsonify({'error': '不支持的文件类型'}), 400


@app.route('/api/transcribe', methods=['POST'])
def transcribe_audio():
    """语音转文字：接收音频文件或已上传的 file_id，返回文本"""
    if 'file' in request.files:
        # 直接上传音频文件
        audio = request.files['file']
        if audio.filename == '':
            return jsonify({'error': '没有选择音频文件'}), 400
        # 存临时路径
        temp_name = secure_filename(audio.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"tmp_{uuid.uuid4()}_{temp_name}")
        audio.save(temp_path)
        try:
            text = _transcribe_file_local(temp_path)
            return jsonify({'text': text})
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    else:
        data = request.get_json() or {}
        file_id = data.get('file_id')
        if not file_id or file_id not in uploaded_files:
            return jsonify({'error': '缺少有效的 file_id 或文件不存在'}), 400
        file_path = uploaded_files[file_id]['path']
        # 校验扩展名
        ext = os.path.splitext(file_path)[1].lower().strip('.')
        if ext not in {'mp3','wav','m4a','webm','ogg'}:
            return jsonify({'error': '该文件不是受支持的音频类型'}), 400
        text = _transcribe_file_local(file_path)
        return jsonify({'text': text})


@app.route('/api/files', methods=['GET'])
def get_files():
    """获取已上传文件列表"""
    files_list = []
    for file_id, info in uploaded_files.items():
        files_list.append({
            'id': file_id,
            'name': info['name'],
            'upload_time': info['upload_time']
        })
    return jsonify({'files': files_list})


@app.route('/api/files/<file_id>', methods=['DELETE'])
def delete_file(file_id):
    """删除文件"""
    if file_id in uploaded_files:
        file_info = uploaded_files[file_id]
        # 删除本地文件
        if os.path.exists(file_info['path']):
            os.remove(file_info['path'])
        # 从内存中删除
        del uploaded_files[file_id]
        return jsonify({'message': '文件删除成功'})
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/conversations', methods=['GET'])
def get_conversations():
    """获取对话历史列表"""
    conversations = []
    for conv_id, messages in conversation_history.items():
        if messages:
            first_message = messages[0]['message']
            # 截取前50个字符作为标题
            title = first_message[:50] + '...' if len(first_message) > 50 else first_message
            conversations.append({
                'id': conv_id,
                'title': title,
                'last_message_time': messages[-1]['timestamp'],
                'message_count': len(messages)
            })
    
    # 按时间排序
    conversations.sort(key=lambda x: x['last_message_time'], reverse=True)
    return jsonify({'conversations': conversations})


@app.route('/api/conversations/<conversation_id>', methods=['GET'])
def get_conversation(conversation_id):
    """获取特定对话的详细内容"""
    if conversation_id in conversation_history:
        return jsonify({'messages': conversation_history[conversation_id]})
    return jsonify({'error': '对话不存在'}), 404


@app.route('/api/conversations/<conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id):
    """删除对话"""
    if conversation_id in conversation_history:
        del conversation_history[conversation_id]
        _save_conversations()
        return jsonify({'message': '对话删除成功'})
    return jsonify({'error': '对话不存在'}), 404


# =============================================================================
# MCP 管理 API（校验+CRUD，占位实现内存存储）
# =============================================================================

@app.route('/api/mcps', methods=['GET'])
def list_mcps():
    return jsonify({'mcps': list(mcp_registry.values())})


@app.route('/api/mcps', methods=['POST'])
def create_mcp():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    enabled = bool(data.get('enabled', True))
    config = data.get('config') or {}

    if not name:
        return jsonify({'error': '名称不能为空'}), 400

    # 校验连通性
    ok, err = validate_mcp_connectivity(config)
    if not ok:
        return jsonify({'error': f'校验失败：{err}'}), 400

    mcp_id = str(uuid.uuid4())
    mcp_info = {
        'id': mcp_id,
        'name': name,
        'description': description,
        'enabled': enabled,
        'config': config,
        'created_at': datetime.now().isoformat()
    }
    mcp_registry[mcp_id] = mcp_info
    _save_mcp_registry()
    return jsonify({'message': '创建成功', 'mcp': mcp_info})


@app.route('/api/mcps/<mcp_id>', methods=['PUT'])
def update_mcp(mcp_id):
    if mcp_id not in mcp_registry:
        return jsonify({'error': 'MCP 不存在'}), 404
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    enabled = bool(data.get('enabled', True))
    config = data.get('config') or {}

    if not name:
        return jsonify({'error': '名称不能为空'}), 400

    # 修改配置时也校验连通性
    ok, err = validate_mcp_connectivity(config)
    if not ok:
        return jsonify({'error': f'校验失败：{err}'}), 400

    mcp = mcp_registry[mcp_id]
    mcp.update({
        'name': name,
        'description': description,
        'enabled': enabled,
        'config': config,
        'updated_at': datetime.now().isoformat()
    })
    _save_mcp_registry()
    return jsonify({'message': '更新成功', 'mcp': mcp})


@app.route('/api/mcps/<mcp_id>/enable', methods=['PATCH'])
def enable_mcp(mcp_id):
    if mcp_id not in mcp_registry:
        return jsonify({'error': 'MCP 不存在'}), 404
    data = request.get_json() or {}
    enabled = bool(data.get('enabled', True))
    mcp_registry[mcp_id]['enabled'] = enabled
    mcp_registry[mcp_id]['updated_at'] = datetime.now().isoformat()
    _save_mcp_registry()
    return jsonify({'message': '状态已更新'})


@app.route('/api/mcps/<mcp_id>', methods=['DELETE'])
def delete_mcp_api(mcp_id):
    if mcp_id not in mcp_registry:
        return jsonify({'error': 'MCP 不存在'}), 404
    del mcp_registry[mcp_id]
    _save_mcp_registry()
    return jsonify({'message': '删除成功'})


@app.route('/api/clear', methods=['POST'])
def clear_all():
    """清除所有数据"""
    conversation_history.clear()
    # 清理上传的文件
    for file_id, info in uploaded_files.items():
        if os.path.exists(info['path']):
            os.remove(info['path'])
    uploaded_files.clear()
    _save_conversations()
    return jsonify({'message': '所有数据已清除'})


# =============================================================================
# 主程序入口
# =============================================================================

# 初始化AI应用
ai_app = AIWebApp()
_load_mcp_registry()
_load_conversations()

if __name__ == '__main__':
    print("🎓 启动AI全能教师Web系统...")
    print("🌐 访问地址: http://localhost:5000")
    print("📁 支持文件上传功能")
    print("💬 支持多轮对话交互")
    print("🔧 支持多智能体协作和工具调用")
    print("🚀 系统已就绪，开始服务...")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
