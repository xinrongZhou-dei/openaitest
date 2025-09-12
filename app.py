import os
import json
import asyncio
import threading
from datetime import datetime
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import openai
from typing import Literal, Union, TYPE_CHECKING, Any
from playwright.async_api import Browser, Page, Playwright, async_playwright
from agents import Agent, Runner, AsyncComputer, ComputerTool, ModelSettings, Button, Environment
from agents.realtime import RealtimeRunner, RealtimeSession, RealtimeSessionEvent
from agents.realtime.config import RealtimeUserInputMessage
from agents.realtime.model_inputs import RealtimeModelSendRawMessage
from pydantic import BaseModel
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename
import uuid
import struct
import base64
import logging

# 设置OpenAI API Key
os.environ["OPENAI_API_KEY"] = "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"

# 初始化OpenAI客户端
client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 创建Flask应用
app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # 用于session管理

# 初始化SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# 配置
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'txt', 'doc', 'docx', 'py', 'js', 'java', 'cpp', 'c', 'csv', 'xlsx', 'xls'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# 确保上传目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 全局变量存储对话历史和文件信息
conversation_history = {}
uploaded_files = {}

# 通话相关全局变量
active_calls = {}  # 存储活跃的通话会话

# Realtime API 管理器
class RealtimeWebSocketManager:
    def __init__(self):
        self.active_sessions: dict[str, RealtimeSession] = {}
        self.session_contexts: dict[str, Any] = {}
        self.websockets: dict[str, str] = {}  # session_id -> socketio_sid

    async def connect(self, session_id: str, socketio_sid: str):
        """连接到Realtime API"""
        try:
            # 获取智能体
            agent = get_starting_agent()
            runner = RealtimeRunner(agent)
            
            # 设置中文语言配置
            model_config = {
                "initial_model_settings": {
                    "instructions": "请始终用中文回复用户，保持友好和专业的语调。",
                    "modalities": ["text", "audio"]
                }
            }
            
            session_context = await runner.run(model_config=model_config)
            session = await session_context.__aenter__()
            
            self.active_sessions[session_id] = session
            self.session_contexts[session_id] = session_context
            self.websockets[session_id] = socketio_sid

            # 启动事件处理任务
            asyncio.create_task(self._process_events(session_id))
            
            logger.info(f"Realtime session {session_id} connected")
            
        except Exception as e:
            logger.error(f"Failed to connect Realtime session {session_id}: {e}")
            raise

    async def disconnect(self, session_id: str):
        """断开Realtime API连接"""
        try:
            if session_id in self.session_contexts:
                await self.session_contexts[session_id].__aexit__(None, None, None)
                del self.session_contexts[session_id]
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
            if session_id in self.websockets:
                del self.websockets[session_id]
            logger.info(f"Realtime session {session_id} disconnected")
        except Exception as e:
            logger.error(f"Error disconnecting session {session_id}: {e}")

    async def send_audio(self, session_id: str, audio_bytes: bytes):
        """发送音频到Realtime API"""
        if session_id in self.active_sessions:
            await self.active_sessions[session_id].send_audio(audio_bytes)

    async def send_user_message(self, session_id: str, message: RealtimeUserInputMessage):
        """发送用户消息到Realtime API"""
        if session_id in self.active_sessions:
            await self.active_sessions[session_id].send_message(message)

    async def interrupt(self, session_id: str):
        """中断当前播放"""
        if session_id in self.active_sessions:
            await self.active_sessions[session_id].interrupt()

    async def _process_events(self, session_id: str):
        """处理Realtime API事件"""
        try:
            session = self.active_sessions[session_id]
            socketio_sid = self.websockets[session_id]

            async for event in session:
                event_data = await self._serialize_event(event)
                
                # 处理语音识别结果
                if event.type == "history_added" and event.item:
                    item = event.item
                    if hasattr(item, 'role') and item.role == 'user' and hasattr(item, 'content'):
                        # 提取用户语音识别的文本
                        text_content = ""
                        if isinstance(item.content, list):
                            for content_item in item.content:
                                if hasattr(content_item, 'type') and content_item.type == 'text':
                                    text_content = content_item.text
                                    break
                        elif isinstance(item.content, str):
                            text_content = item.content
                        
                        if text_content:
                            # 将语音识别的文本传递给中心智能体系统
                            await self._process_voice_text(session_id, text_content, socketio_sid)
                
                # 通过SocketIO发送事件到前端
                socketio.emit('realtime_event', event_data, room=socketio_sid)
                
        except Exception as e:
            logger.error(f"Error processing events for session {session_id}: {e}")

    async def _process_voice_text(self, session_id: str, text: str, socketio_sid: str):
        """处理语音识别的文本，传递给中心智能体系统"""
        try:
            logger.info(f"Processing voice text: {text}")
            
            # 创建语音对话ID
            voice_conversation_id = f"voice_{session_id}"
            
            # 使用现有的AI处理系统
            response = ai_app.process_ai_response(
                user_message=text,
                conversation_id=voice_conversation_id,
                use_web_search=False,  # 语音识别默认不联网搜索
                region="auto",
                file_id=None
            )
            
            # 发送智能体回复给前端
            socketio.emit('agent_response', {
                'text': response,
                'session_id': session_id,
                'timestamp': datetime.now().isoformat()
            }, room=socketio_sid)
            
            logger.info(f"Agent response sent: {response[:100]}...")
            
        except Exception as e:
            logger.error(f"Error processing voice text: {e}")
            socketio.emit('agent_error', {
                'error': str(e),
                'session_id': session_id,
                'timestamp': datetime.now().isoformat()
            }, room=socketio_sid)

    async def _serialize_event(self, event: RealtimeSessionEvent) -> dict[str, Any]:
        """序列化Realtime事件"""
        base_event: dict[str, Any] = {
            "type": event.type,
        }

        if event.type == "agent_start":
            base_event["agent"] = event.agent.name
        elif event.type == "agent_end":
            base_event["agent"] = event.agent.name
        elif event.type == "handoff":
            base_event["from"] = event.from_agent.name
            base_event["to"] = event.to_agent.name
        elif event.type == "tool_start":
            base_event["tool"] = event.tool.name
        elif event.type == "tool_end":
            base_event["tool"] = event.tool.name
            base_event["output"] = str(event.output)
        elif event.type == "audio":
            base_event["audio"] = base64.b64encode(event.audio.data).decode("utf-8")
        elif event.type == "audio_interrupted":
            pass
        elif event.type == "audio_end":
            pass
        elif event.type == "history_updated":
            base_event["history"] = [item.model_dump(mode="json") for item in event.history]
        elif event.type == "history_added":
            try:
                base_event["item"] = event.item.model_dump(mode="json")
            except Exception:
                base_event["item"] = None
        elif event.type == "guardrail_tripped":
            base_event["guardrail_results"] = [
                {"name": result.guardrail.name} for result in event.guardrail_results
            ]
        elif event.type == "raw_model_event":
            base_event["raw_model_event"] = {
                "type": event.data.type,
            }
        elif event.type == "error":
            base_event["error"] = str(event.error) if hasattr(event, "error") else "Unknown error"
        elif event.type == "input_audio_timeout_triggered":
            pass

        return base_event

# 初始化Realtime管理器
realtime_manager = RealtimeWebSocketManager()

def get_starting_agent():
    """获取起始智能体 - 使用现有的中心智能体系统"""
    # 创建一个简单的智能体，它只负责接收语音输入并转发给中心智能体
    from agents import Agent
    
    agent = Agent(
        name="Voice Input Handler",
        instructions="""你是一个语音输入处理器，负责接收用户的语音输入并转发给中心智能体系统。
        
        你的职责：
        1. 接收用户的语音输入
        2. 将语音内容转发给中心智能体系统
        3. 让中心智能体判断由哪个专业智能体回复
        
        请始终用中文回复，保持友好和专业的语调。""",
    )
    
    return agent

class LocalPlaywrightComputer(AsyncComputer):
    """基于本地Playwright浏览器的联网搜索计算机工具。"""

    def __init__(self, start_url: str = "https://www.bing.com"):
        self._playwright: Union[Playwright, None] = None
        self._browser: Union[Browser, None] = None
        self._page: Union[Page, None] = None
        self._start_url = start_url

    async def _get_browser_and_page(self) -> tuple[Browser, Page]:
        width, height = self.dimensions
        # 无头 + 容器兼容参数，避免在无GUI/受限环境下启动失败
        launch_args = [
            f"--window-size={width},{height}",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ]
        try:
            browser = await self.playwright.chromium.launch(headless=True, args=launch_args)
        except Exception as e:
            raise RuntimeError(
                "Playwright 启动 Chromium 失败。请先在终端执行一次：\n"
                "python -m playwright install chromium\n\n"
                f"原始错误：{e}"
            )
        page = await browser.new_page()
        await page.set_viewport_size({"width": width, "height": height})
        await page.goto(self._start_url, timeout=30000)
        return browser, page

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser, self._page = await self._get_browser_and_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @property
    def playwright(self) -> Playwright:
        assert self._playwright is not None
        return self._playwright

    @property
    def browser(self) -> Browser:
        assert self._browser is not None
        return self._browser

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page

    @property
    def environment(self) -> Environment:
        return "browser"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (1024, 768)

    async def screenshot(self) -> str:
        png_bytes = await self.page.screenshot(full_page=False)
        import base64
        return base64.b64encode(png_bytes).decode("utf-8")

    async def click(self, x: int, y: int, button: Button = "left") -> None:
        playwright_button: Literal["left", "middle", "right"] = "left"
        if button in ("left", "right", "middle"):
            playwright_button = button  # type: ignore
        await self.page.mouse.click(x, y, button=playwright_button)

    async def double_click(self, x: int, y: int) -> None:
        await self.page.mouse.dblclick(x, y)

    async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        await self.page.mouse.move(x, y)
        await self.page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    async def type(self, text: str) -> None:
        await self.page.keyboard.type(text)

    async def wait(self) -> None:
        await asyncio.sleep(1)

    async def move(self, x: int, y: int) -> None:
        await self.page.mouse.move(x, y)

    async def keypress(self, keys: list[str]) -> None:
        CUA_KEY_TO_PLAYWRIGHT_KEY = {
            "/": "Divide",
            "\\": "Backslash",
            "alt": "Alt",
            "arrowdown": "ArrowDown",
            "arrowleft": "ArrowLeft",
            "arrowright": "ArrowRight",
            "arrowup": "ArrowUp",
            "backspace": "Backspace",
            "capslock": "CapsLock",
            "cmd": "Meta",
            "ctrl": "Control",
            "delete": "Delete",
            "end": "End",
            "enter": "Enter",
            "esc": "Escape",
            "home": "Home",
            "insert": "Insert",
            "option": "Alt",
            "pagedown": "PageDown",
            "pageup": "PageUp",
            "shift": "Shift",
            "space": " ",
            "super": "Meta",
            "tab": "Tab",
            "win": "Meta",
        }
        mapped_keys = [CUA_KEY_TO_PLAYWRIGHT_KEY.get(key.lower(), key) for key in keys]
        for key in mapped_keys:
            await self.page.keyboard.down(key)
        for key in reversed(mapped_keys):
            await self.page.keyboard.up(key)

    async def drag(self, path: list[tuple[int, int]]) -> None:
        if not path:
            return
        await self.page.mouse.move(path[0][0], path[0][1])
        await self.page.mouse.down()
        for px, py in path[1:]:
            await self.page.mouse.move(px, py)
        await self.page.mouse.up()


class AIWebApp:
    def __init__(self):
        self.setup_agents()
    
    def setup_agents(self):
        """设置agents系统"""
        # 中心协调智能体
        self.central_coordinator_agent = Agent(
            name="Central Coordinator",
            instructions="""你是一个智能协调中心，负责分析用户意图并调度合适的专业智能体。

你的核心职责：
1. **意图分析**: 深入理解用户消息的真实意图和需求
2. **智能路由**: 根据意图选择最合适的专业智能体组合
3. **任务分解**: 将复杂任务分解为多个子任务
4. **结果整合**: 整合多个智能体的回答，提供统一回复

可调度的专业智能体：
- Universal Teacher: 通用教学和知识问答
- File Analysis Expert: 文件内容分析
- Web Search Agent (CN): 国内网络搜索
- Web Search Agent (Global): 国际网络搜索

协作处理能力：
- 文件分析 + 联网搜索：先分析文件内容，再搜索相关信息
- 教学 + 搜索：结合知识教学和最新信息
- 多轮对话 + 文件处理：结合对话历史和文件内容

决策原则：
- 优先分析用户真实意图，而非简单关键词匹配
- 考虑任务复杂度和所需专业能力
- 支持多智能体协作处理复杂任务
- 确保回答的准确性和完整性

请用中文回答，语言要清晰准确，适合教学使用。""",
        )
        
        # 全能教师智能体
        self.universal_teacher_agent = Agent(
            name="Universal Teacher",
            instructions="""你是一个全能的AI教师，能够教授各种学科和知识领域。

你的教学能力包括：
1. **数学教学**: 代数、几何、微积分、统计学等
2. **科学教学**: 物理、化学、生物、天文学等
3. **语言教学**: 中文、英文、文学、写作等
4. **历史教学**: 世界历史、中国历史、文化史等
5. **艺术教学**: 音乐、美术、设计、艺术史等
6. **技术教学**: 编程、计算机科学、人工智能等
7. **生活技能**: 烹饪、园艺、手工、生活技巧等
8. **哲学思考**: 逻辑思维、批判性思维、伦理学等

网络搜索能力：
- 你可以使用网络搜索来获取最新信息
- 对于需要实时数据的问题（如最新新闻、当前事件、实时数据等），请主动使用网络搜索
- 结合网络搜索结果和你的知识库，提供最准确和最新的教学信息

教学原则：
- 根据学生的理解水平调整教学深度
- 提供清晰的解释和具体的例子
- 鼓励学生思考和提问
- 使用生动有趣的教学方法
- 注重知识的实际应用
- 培养学生的学习兴趣和好奇心
- 优先使用最新和准确的信息进行教学

请用中文回答，语言要生动有趣，适合教学使用。根据问题类型提供相应的专业知识和教学方法。当需要最新信息时，请使用网络搜索功能。""",
        )
        
        # 文件分析智能体
        self.file_analysis_agent = Agent(
            name="File Analysis Expert",
            instructions="""你是一个专业的文件分析专家，专门负责分析和解读各种类型的文件内容。

你的分析能力包括：
1. **文档分析**: PDF、Word、TXT等文档的内容提取和总结
2. **代码分析**: 编程代码的结构分析、功能解释和优化建议
3. **数据文件**: Excel、CSV等数据文件的统计分析和可视化建议
4. **图片分析**: 图片内容的描述和分析（如果支持）
5. **学术论文**: 论文结构、研究方法、结论等分析
6. **报告解读**: 商业报告、技术报告等的要点提取

分析原则：
- 准确理解文件内容，提取关键信息
- 提供清晰的结构化分析结果
- 针对用户的具体问题给出针对性回答
- 使用通俗易懂的语言解释专业内容
- 提供实用的建议和见解
- 保持客观中立的分析态度

请用中文回答，语言要清晰准确，适合教学使用。根据文件类型和用户问题提供专业的分析服务。""",
        )
        
        # 联网搜索智能体（国际）
        self.web_search_agent_global = Agent(
            name="Web Search Agent (Global)",
            instructions="You are a helpful research assistant. Use the browser tool to search the web and summarize answers in Chinese with citations to sources (site names).",
            tools=[ComputerTool(LocalPlaywrightComputer(start_url="https://www.bing.com"))],
            model="computer-use-preview",
            model_settings=ModelSettings(truncation="auto"),
        )

        # 联网搜索智能体（国内）
        self.web_search_agent_cn = Agent(
            name="Web Search Agent (CN)",
            instructions="You are a helpful research assistant. Use the browser tool to search the web and summarize answers in Chinese with citations to sources (site names). Prefer Chinese sources.",
            tools=[ComputerTool(LocalPlaywrightComputer(start_url="https://www.baidu.com"))],
            model="computer-use-preview",
            model_settings=ModelSettings(truncation="auto"),
        )
        
        self.agents_ready = True

    def analyze_task_complexity(self, user_message, file_id, use_web_search, region="auto"):
        """分析任务复杂度和意图"""
        analysis = {
            'needs_file_analysis': bool(file_id),
            'needs_web_search': use_web_search,
            'needs_collaboration': False,
            'task_type': 'simple',
            'file_id': file_id,
            'region': region,
            'keywords': {
                'file_keywords': ['分析', '文件', '内容', '总结', '解读', '查看', '文档', '代码'],
                'search_keywords': ['最新', '当前', '现在', '实时', '新闻', '发展', '趋势', '更新'],
                'teaching_keywords': ['解释', '教学', '学习', '如何', '为什么', '什么是', '怎么']
            }
        }
        
        # 检测关键词
        message_lower = user_message.lower()
        has_file_keywords = any(keyword in message_lower for keyword in analysis['keywords']['file_keywords'])
        has_search_keywords = any(keyword in message_lower for keyword in analysis['keywords']['search_keywords'])
        has_teaching_keywords = any(keyword in message_lower for keyword in analysis['keywords']['teaching_keywords'])
        
        # 判断任务类型
        if analysis['needs_file_analysis'] and analysis['needs_web_search']:
            # 文件分析 + 联网搜索
            analysis['needs_collaboration'] = True
            analysis['task_type'] = 'file_analysis + web_search'
        elif analysis['needs_file_analysis'] and has_search_keywords:
            # 文件分析 + 搜索关键词（需要协作）
            analysis['needs_collaboration'] = True
            analysis['task_type'] = 'file_analysis + web_search'
        elif analysis['needs_file_analysis']:
            # 纯文件分析
            analysis['task_type'] = 'file_analysis'
        elif analysis['needs_web_search']:
            # 纯联网搜索
            analysis['task_type'] = 'web_search'
        elif has_teaching_keywords or not (has_file_keywords or has_search_keywords):
            # 教学任务
            analysis['task_type'] = 'teaching'
        else:
            # 默认教学
            analysis['task_type'] = 'teaching'
        
        return analysis

    def select_agents_for_task(self, task_analysis):
        """根据任务分析选择智能体"""
        selected_agents = []
        
        if task_analysis['task_type'] == 'file_analysis + web_search':
            # 需要协作：文件分析 + 联网搜索
            selected_agents = ['file_analysis', 'web_search']
        elif task_analysis['task_type'] == 'file_analysis':
            # 文件分析
            selected_agents = ['file_analysis']
        elif task_analysis['task_type'] == 'web_search':
            # 联网搜索
            selected_agents = ['web_search']
        else:
            # 教学任务
            selected_agents = ['teaching']
        
        return selected_agents

    async def execute_agent_task(self, agent_name, user_message, context, task_analysis):
        """执行单个智能体任务"""
        try:
            if agent_name == 'file_analysis':
                return self.analyze_file_with_openai(user_message, task_analysis['file_id'])
            
            elif agent_name == 'web_search':
                # 选择搜索智能体
                if task_analysis['region'] == 'cn':
                    agent = self.web_search_agent_cn
                elif task_analysis['region'] == 'global':
                    agent = self.web_search_agent_global
                else:
                    # auto: 中文输入优先国内
                    if any(ch for ch in user_message if '\u4e00' <= ch <= '\u9fff'):
                        agent = self.web_search_agent_cn
                    else:
                        agent = self.web_search_agent_global

                # 首选浏览器搜索；失败则回退到 API 搜索
                try:
                    result = await Runner.run(agent, user_message)
                    return result.final_output
                except Exception as run_err:
                    fallback = self.call_openai_api(user_message, conversation_id="web_search_fallback")
                    if isinstance(fallback, str) and fallback.strip():
                        return fallback
                    return f"联网搜索失败：{run_err}"
            
            elif agent_name == 'teaching':
                result = await Runner.run(self.universal_teacher_agent, user_message)
                return result.final_output
            
            else:
                return f"未知的智能体类型: {agent_name}"
                
        except Exception as e:
            return f"智能体 {agent_name} 执行失败: {str(e)}"

    async def process_collaborative_task(self, user_message, conversation_id, task_analysis):
        """处理协作任务"""
        results = {}
        
        # 1. 先进行文件分析
        if 'file_analysis' in task_analysis['selected_agents']:
            print("执行文件分析...")
            file_result = await self.execute_agent_task('file_analysis', user_message, None, task_analysis)
            results['file_analysis'] = file_result
            
            # 2. 基于文件分析结果进行联网搜索
            if 'web_search' in task_analysis['selected_agents']:
                print("基于文件分析结果进行联网搜索...")
                # 构建搜索查询，结合文件内容和用户问题
                search_query = f"""
                基于以下文件分析结果：
                {file_result}
                
                用户问题：{user_message}
                
                请搜索相关信息并补充回答。
                """
                
                # 执行联网搜索
                web_result = await self.execute_agent_task('web_search', search_query, None, task_analysis)
                results['web_search'] = web_result
        
        # 3. 整合结果
        final_response = self.integrate_collaborative_results(results, user_message, task_analysis)
        return final_response

    def integrate_collaborative_results(self, results, user_message, task_analysis):
        """整合协作结果"""
        if 'file_analysis' in results and 'web_search' in results:
            # 文件分析 + 联网搜索
            integrated_response = f"""
## 📄 文件分析结果
{results['file_analysis']}

---

## 🌐 最新信息补充
{results['web_search']}

---

## 💡 综合回答
基于文件内容和最新信息，为您提供完整的回答。如果您需要更详细的信息或有其他问题，请随时告诉我！
            """
            return integrated_response
        elif 'file_analysis' in results:
            # 只有文件分析
            return results['file_analysis']
        elif 'web_search' in results:
            # 只有联网搜索
            return results['web_search']
        else:
            # 其他情况
            return list(results.values())[0] if results else "处理失败"

    def analyze_file_with_openai(self, user_message, file_id):
        """使用OpenAI API分析文件（按类型分流）"""
        try:
            if not file_id or file_id not in uploaded_files:
                return "请先上传一个文件进行分析。"
            
            file_info = uploaded_files[file_id]
            file_name = file_info['name']
            file_path = file_info['path']
            ext = os.path.splitext(file_name)[1].lower()
            
            # 支持的文本/代码扩展名
            text_like_exts = {
                '.txt', '.md', '.csv', '.json', '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.c', '.cpp', '.cs',
                '.html', '.css', '.yml', '.yaml', '.ini', '.cfg', '.toml', '.sql'
            }
            office_exts = {'.doc', '.docx', '.xlsx', '.xls', '.ppt', '.pptx'}
            
            if ext == '.pdf':
                # PDF 走 input_file 流程
                input_content = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_file", "file_id": file_id},
                            {
                                "type": "input_text",
                                "text": (
                                    f"请分析这个PDF文件：{file_name}\n\n用户问题：{user_message}\n\n"
                                    "请用中文回答，语言要清晰准确，并给出结构化要点。"
                                ),
                            },
                        ],
                    }
                ]
                response = client.responses.create(
                    model="gpt-4o",
                    input=input_content,
                    temperature=0.7,
                    max_output_tokens=2000,
                )
            elif ext in text_like_exts:
                # 文本/代码直接读取内容作为 input_text
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                except Exception as read_err:
                    return f"读取文件失败：{read_err}"
                
                # 简单长度控制，避免超长
                max_chars = 80000
                truncated = False
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n\n[内容过长，已截断，仅分析前面部分]"
                    truncated = True
                
                full_prompt = (
                    f"文件名：{file_name}\n\n"
                    f"文件内容（可能已截断）：\n{content}\n\n"
                    f"用户问题：{user_message}\n\n"
                    "请基于以上文件内容进行分析与回答。用中文输出，给出清晰的小标题与要点列表。"
                )
                response = client.responses.create(
                    model="gpt-4o",
                    input=[{"role": "user", "content": [{"type": "input_text", "text": full_prompt}]}],
                    temperature=0.7,
                    max_output_tokens=2000,
                )
            elif ext in office_exts:
                return (
                    "当前文件类型暂不支持原文上传解析。建议：\n"
                    "- 将Word/Excel/PPT导出为PDF后再上传；或\n"
                    "- 复制关键信息为文本（.txt/.md/.csv/.json/代码）后再上传。"
                )
            else:
                return (
                    f"暂不支持的文件类型：{ext}。建议将文件导出为PDF或文本后再试。"
                )
            
            # 结果解析：优先 output_text
            try:
                text = getattr(response, 'output_text', None)
                if text:
                    return text
            except Exception:
                pass
            
            # 兼容兜底
            try:
                parts = []
                for item in getattr(response, 'output', []) or []:
                    for c in getattr(item, 'content', []) or []:
                        if isinstance(c, dict) and c.get('type') in ('output_text', 'text') and c.get('text'):
                            parts.append(c['text'])
                if parts:
                    return "\n".join(parts)
            except Exception:
                pass
            
            return str(response)
        except Exception as e:
            return f"文件分析时出现错误：{str(e)}"

    def call_openai_api(self, user_message, conversation_id):
        """调用OpenAI Responses API 并启用联网搜索（web_search_preview）"""
        try:
            # 构建单条输入文本，兼容Responses API的 input 形态
            system_prompt = (
                "你是一个全能的AI教师，能够教授各种学科和知识领域。"
                "请用中文回答，语言要生动有趣，适合教学使用。"
                "根据问题类型提供相应的专业知识和教学方法。"
                "当需要最新信息时，请使用网络搜索功能，并标注关键信息来源。"
            )

            # 将最近对话历史拼接为上下文（最多5轮）
            recent_history = conversation_history.get(conversation_id, [])[-10:]
            history_text_parts = []
            for msg in recent_history:
                if msg['type'] == 'user':
                    history_text_parts.append(f"用户: {msg['message']}")
                elif msg['type'] == 'assistant':
                    history_text_parts.append(f"教师: {msg['message']}")

            history_block = "\n".join(history_text_parts).strip()
            input_text = (
                f"系统: {system_prompt}\n\n"
                f"历史对话:\n{history_block}\n\n" if history_block else f"系统: {system_prompt}\n\n"
            ) + f"用户: {user_message}"

            # 首选使用支持预览联网搜索工具的模型与工具名（与您示例一致）
            def run_with_model(model_name):
                return client.responses.create(
                    model=model_name,
                    tools=[{"type": "web_search_preview"}],
                    input=input_text,
                    tool_choice="auto",
                    temperature=0.7,
                    max_output_tokens=1500,
                )

            resp = None
            last_err = None
            for candidate_model in ["gpt-5", "gpt-4.1", "gpt-4.1-mini", "gpt-4o"]:
                try:
                    resp = run_with_model(candidate_model)
                    if resp:
                        break
                except Exception as e:
                    last_err = e
                    continue

            if resp is None and last_err is not None:
                # 回退：去掉联网搜索工具，直接用通用模型给出回答，避免整体失败
                try:
                    resp = client.responses.create(
                        model="gpt-4o",
                        input=input_text,
                        temperature=0.7,
                        max_output_tokens=1500,
                    )
                except Exception:
                    raise last_err

            # 优先使用 output_text（新SDK提供）
            text = getattr(resp, 'output_text', None)
            if text:
                return text

            # 兼容兜底：从 output 结构中拼接
            parts = []
            try:
                for item in getattr(resp, 'output', []) or []:
                    for c in getattr(item, 'content', []) or []:
                        if isinstance(c, dict) and c.get('type') in ('output_text', 'text') and c.get('text'):
                            parts.append(c['text'])
                if parts:
                    return "\n".join(parts)
            except Exception:
                pass

            # 再兜底：旧结构
            try:
                return resp.choices[0].message.content
            except Exception:
                return str(resp)

        except Exception as e:
            return f"抱歉，调用OpenAI API时出现错误：{str(e)}"

    def process_ai_response(self, user_message, conversation_id, use_web_search=False, region="auto", file_id=None):
        """处理AI响应 - 使用中心智能体协调系统"""
        try:
            if not self.agents_ready:
                return "抱歉，AI教师系统暂时不可用。"
            
            # 调试信息
            print(f"收到消息: {user_message}")
            print(f"文件ID: {file_id}")
            print(f"已上传文件: {list(uploaded_files.keys())}")
            print(f"联网搜索: {use_web_search}")
            print(f"区域: {region}")
            
            # 1. 分析任务复杂度
            task_analysis = self.analyze_task_complexity(user_message, file_id, use_web_search, region)
            print(f"任务分析: {task_analysis}")
            
            # 2. 选择智能体
            selected_agents = self.select_agents_for_task(task_analysis)
            task_analysis['selected_agents'] = selected_agents
            print(f"选择的智能体: {selected_agents}")
            
            # 3. 执行任务
            if task_analysis['needs_collaboration']:
                # 协作任务
                print("执行协作任务...")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    response = loop.run_until_complete(
                        self.process_collaborative_task(user_message, conversation_id, task_analysis)
                    )
                finally:
                    loop.close()
            else:
                # 单一任务
                print(f"执行单一任务: {task_analysis['task_type']}")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    agent_name = selected_agents[0] if selected_agents else 'teaching'
                    response = loop.run_until_complete(
                        self.execute_agent_task(agent_name, user_message, None, task_analysis)
                    )
                finally:
                    loop.close()
            
            print(f"处理完成，响应长度: {len(response) if response else 0}")
            return response
            
        except Exception as e:
            print(f"处理请求时出现错误：{str(e)}")
            return f"处理请求时出现错误：{str(e)}"

# 初始化AI应用
ai_app = AIWebApp()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/call')
def call():
    return render_template('call.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_message = data.get('message', '').strip()
    conversation_id = data.get('conversation_id', str(uuid.uuid4()))
    use_web_search = data.get('use_web_search', False)
    region = data.get('region', 'auto')
    file_id = data.get('file_id')
    
    if not user_message:
        return jsonify({'error': '消息不能为空'}), 400
    
    # 获取或创建对话历史
    if conversation_id not in conversation_history:
        conversation_history[conversation_id] = []
    
    # 添加用户消息到历史
    conversation_history[conversation_id].append({
        'timestamp': datetime.now().isoformat(),
        'type': 'user',
        'message': user_message
    })
    
    # 调试信息
    print(f"收到消息: {user_message}")
    print(f"文件ID: {file_id}")
    print(f"已上传文件: {list(uploaded_files.keys())}")
    
    # 处理AI响应
    response = ai_app.process_ai_response(user_message, conversation_id, use_web_search, region, file_id)


    # 添加AI回复到历史
    conversation_history[conversation_id].append({
        'timestamp': datetime.now().isoformat(),
        'type': 'assistant',
        'message': response
    })
    
    return jsonify({
        'response': response,
        'conversation_id': conversation_id
    })

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': '没有选择文件'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '没有选择文件'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # 添加时间戳避免文件名冲突
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # 上传到OpenAI
        try:
            with open(filepath, "rb") as f:
                openai_response = client.files.create(
                    file=f,
                    purpose="user_data"
                )
            
            file_id = openai_response.id
            
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
        except Exception as e:
            # 删除本地文件
            if os.path.exists(filepath):
                os.remove(filepath)
            return jsonify({'error': f'文件上传失败：{str(e)}'}), 500
    
    return jsonify({'error': '不支持的文件类型'}), 400

@app.route('/api/files', methods=['GET'])
def get_files():
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
    if file_id in uploaded_files:
        file_info = uploaded_files[file_id]
        # 删除本地文件
        if os.path.exists(file_info['path']):
            os.remove(file_info['path'])
        # 从OpenAI删除
        try:
            client.files.delete(file_id)
        except:
            pass
        # 从内存中删除
        del uploaded_files[file_id]
        return jsonify({'message': '文件删除成功'})
    return jsonify({'error': '文件不存在'}), 404

@app.route('/api/conversations', methods=['GET'])
def get_conversations():
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
    if conversation_id in conversation_history:
        return jsonify({'messages': conversation_history[conversation_id]})
    return jsonify({'error': '对话不存在'}), 404

@app.route('/api/conversations/<conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id):
    if conversation_id in conversation_history:
        del conversation_history[conversation_id]
        return jsonify({'message': '对话删除成功'})
    return jsonify({'error': '对话不存在'}), 404

@app.route('/api/clear', methods=['POST'])
def clear_all():
    conversation_history.clear()
    # 清理上传的文件
    for file_id, info in uploaded_files.items():
        if os.path.exists(info['path']):
            os.remove(info['path'])
        try:
            client.files.delete(file_id)
        except:
            pass
    uploaded_files.clear()
    return jsonify({'message': '所有数据已清除'})

# WebSocket事件处理
@socketio.on('connect')
def handle_connect():
    print(f'用户连接: {request.sid}')
    emit('connected', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    print(f'用户断开连接: {request.sid}')
    # 清理通话会话
    if request.sid in active_calls:
        del active_calls[request.sid]

@socketio.on('join_call')
def handle_join_call(data):
    session_id = data.get('session_id')
    if session_id:
        join_room(session_id)
        active_calls[request.sid] = {
            'session_id': session_id,
            'connected': True
        }
        emit('call_joined', {'session_id': session_id})
        print(f'用户 {request.sid} 加入通话会话 {session_id}')
        
        # 启动Realtime API连接
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(realtime_manager.connect(session_id, request.sid))
            loop.close()
            emit('realtime_connected', {'session_id': session_id})
        except Exception as e:
            print(f'Realtime连接失败: {e}')
            emit('realtime_error', {'error': str(e)})

@socketio.on('leave_call')
def handle_leave_call():
    if request.sid in active_calls:
        session_id = active_calls[request.sid]['session_id']
        leave_room(session_id)
        
        # 断开Realtime API连接
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(realtime_manager.disconnect(session_id))
            loop.close()
        except Exception as e:
            print(f'Realtime断开失败: {e}')
        
        del active_calls[request.sid]
        emit('call_left', {'status': 'left'})
        print(f'用户 {request.sid} 离开通话会话')

@socketio.on('audio_data')
def handle_audio_data(data):
    """处理接收到的音频数据 - 使用Realtime API"""
    audio_data = data.get('audio_data')
    session_id = data.get('session_id')
    
    if audio_data and session_id:
        try:
            # 将int16数组转换为字节
            audio_bytes = struct.pack(f"{len(audio_data)}h", *audio_data)
            
            # 发送音频到Realtime API
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(realtime_manager.send_audio(session_id, audio_bytes))
            loop.close()
            
        except Exception as e:
            print(f'发送音频到Realtime API失败: {e}')
            emit('audio_error', {
                'error': str(e),
                'session_id': session_id
            }, room=session_id)

@socketio.on('audio_response')
def handle_audio_response(data):
    """处理AI生成的音频响应"""
    audio_data = data.get('audio_data')
    session_id = data.get('session_id')
    
    if audio_data and session_id:
        # 广播音频响应给房间内的所有用户
        emit('audio_response', {
            'audio_data': audio_data,
            'timestamp': datetime.now().isoformat()
        }, room=session_id)
        
        print(f'发送音频响应，会话: {session_id}')

if __name__ == '__main__':
    print("🎓 启动AI全能教师Web系统...")
    print("✅ 使用OpenAI API和Agents系统")
    print("🌐 支持网络搜索功能，可获取最新信息")
    print("📁 支持文件上传分析，可分析PDF、代码、文档等")
    print("📚 支持各种学科教学：数学、科学、语言、历史、艺术、技术等")
    print("🔄 支持多轮对话交互")
    print("🌍 访问地址: http://localhost:5000")
    
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
