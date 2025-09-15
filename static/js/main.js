// 全局变量
let currentConversationId = null;
let uploadedFiles = [];
let lastUserMessage = '';

// 通话相关变量
let socket = null;
let isInCall = false;

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    initializeApp();
    loadConversationHistory();
    setupEventListeners();
});

// 初始化应用
function initializeApp() {
    // 设置键盘快捷键
    document.addEventListener('keydown', function(e) {
        if (e.ctrlKey && e.key === 'k') {
            e.preventDefault();
            startNewChat();
        }
    });
    
    // 初始化输入框
    const messageInput = document.getElementById('messageInput');
    messageInput.focus();
}

// 设置事件监听器
function setupEventListeners() {
    // 文件上传区域拖拽事件
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    
    uploadArea.addEventListener('click', () => fileInput.click());
    uploadArea.addEventListener('dragover', handleDragOver);
    uploadArea.addEventListener('drop', handleDrop);
    fileInput.addEventListener('change', handleFileSelect);
}

// 开始新对话
function startNewChat() {
    currentConversationId = null;
    showWelcomeScreen();
    clearMessages();
    document.getElementById('messageInput').focus();
}

// 显示欢迎界面
function showWelcomeScreen() {
    document.getElementById('welcomeScreen').style.display = 'flex';
    document.getElementById('messagesContainer').style.display = 'none';
}

// 隐藏欢迎界面
function hideWelcomeScreen() {
    document.getElementById('welcomeScreen').style.display = 'none';
    document.getElementById('messagesContainer').style.display = 'flex';
}

// 清空消息
function clearMessages() {
    document.getElementById('messagesContainer').innerHTML = '';
}

// 发送消息
async function sendMessage() {
    const messageInput = document.getElementById('messageInput');
    const message = messageInput.value.trim();
    
    if (!message) return;
    
    // 保存最后一条用户消息用于重试
    lastUserMessage = message;
    
    // 调试信息
    console.log('发送消息:', message);
    console.log('已上传文件:', uploadedFiles);
    console.log('文件ID:', uploadedFiles.length > 0 ? uploadedFiles[0].id : null);
    
    // 清空输入框
    messageInput.value = '';
    autoResize(messageInput);
    
    // 隐藏欢迎界面
    hideWelcomeScreen();
    
    // 添加用户消息
    addMessage('user', message);
    
    // 左侧“思考中”指示（放在AI消息位置），并禁用发送按钮
    showTypingIndicator();
    document.getElementById('sendBtn').disabled = true;
    
    try {
        // 发送请求到后端
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message: message,
                conversation_id: currentConversationId,
                // 发送最新一次上传的文件而不是第一个，避免选错文件
                file_id: uploadedFiles.length > 0 ? uploadedFiles[uploadedFiles.length - 1].id : null
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            // 更新对话ID
            currentConversationId = data.conversation_id;
            
            // 添加AI回复
            addMessage('assistant', data.response, data.agent_name, data.tools_used, data.images);
            
            // 更新对话历史
            loadConversationHistory();
        } else {
            addMessage('assistant', `错误: ${data.error}`);
        }
    } catch (error) {
        addMessage('assistant', `网络错误: ${error.message}`);
    } finally {
        // 结束“思考中”指示并恢复按钮
        hideTypingIndicator();
        document.getElementById('sendBtn').disabled = false;
    }
}

// 直接携带文件自动发送（允许空消息，由后端针对音频自动生成默认问题）
async function autoSendWithFile(fileId, prompt = '') {
    // 隐藏欢迎界面并显示用户占位（如果是语音，调用方会已添加语音气泡）
    hideWelcomeScreen();
    showTypingIndicator();
    document.getElementById('sendBtn').disabled = true;
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: prompt,
                conversation_id: currentConversationId,
                file_id: fileId
            })
        });
        const data = await response.json();
        if (response.ok) {
            currentConversationId = data.conversation_id;
            addMessage('assistant', data.response, data.agent_name, data.tools_used, data.images);
            loadConversationHistory();
        } else {
            addMessage('assistant', `错误: ${data.error}`);
        }
    } catch (e) {
        addMessage('assistant', `网络错误: ${e.message}`);
    } finally {
        hideTypingIndicator();
        document.getElementById('sendBtn').disabled = false;
    }
}

// =============== 语音输入 ===============
let mediaRecorder = null;
let chunks = [];
let isRecording = false;
let recordStartMs = 0;

async function toggleRecording() {
    const btn = document.getElementById('recordBtn');
    if (!isRecording) {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            mediaRecorder = new MediaRecorder(stream);
            chunks = [];
            mediaRecorder.ondataavailable = e => { if (e.data && e.data.size > 0) chunks.push(e.data); };
            mediaRecorder.onstop = onRecordingStop;
            mediaRecorder.start();
            recordStartMs = Date.now();
            isRecording = true;
            btn.classList.add('active');
            showNotification('开始录音…');
        } catch (e) {
            showNotification('无法访问麦克风: ' + e.message, 'error');
        }
    } else {
        isRecording = false;
        btn.classList.remove('active');
        if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
    }
}

async function onRecordingStop() {
    try {
        const blob = new Blob(chunks, { type: 'audio/webm' });
        const durationSec = Math.max(1, Math.round((Date.now() - recordStartMs) / 1000));
        const filename = `record_${new Date().toISOString().replace(/[:.]/g,'-')}.webm`;
        await uploadAudioBlob(blob, filename, durationSec);
    } catch (e) {
        showNotification('音频上传失败: ' + e.message, 'error');
    }
}

async function uploadAudioBlob(blob, filename, durationSec) {
    const formData = new FormData();
    const file = new File([blob], filename, { type: 'audio/webm' });
    formData.append('file', file);
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
        showNotification(`语音上传失败: ${data.error || '未知错误'}`, 'error');
        return;
    }
    uploadedFiles.push({ id: data.file_id, name: data.filename, size: blob.size, duration: durationSec, isAudio: true });
    updateUploadedFilesDisplay();
    // 在对话中展示一个语音气泡（不显示文字）
    addMessage('user', `__VOICE__:${durationSec}`);
    showNotification(`语音已录制并上传（${durationSec}s）`, 'success');
    // 自动触发一次对话（后端将根据音频自动转写并生成默认问题）
    await autoSendWithFile(data.file_id, '');
}

// 添加消息到界面
function addMessage(type, content, agentName = null, toolsUsed = [], images = []) {
    const messagesContainer = document.getElementById('messagesContainer');
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${type}`;
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    
    if (type === 'user') {
        avatar.innerHTML = '<i class="fas fa-user"></i>';
    } else {
        avatar.innerHTML = '<i class="fas fa-robot"></i>';
    }
    
    const messageContent = document.createElement('div');
    messageContent.className = 'message-content';
    
    // 如果是AI回复且有智能体名称，显示智能体标签
    if (type === 'assistant' && agentName) {
        const agentLabel = document.createElement('div');
        agentLabel.className = 'agent-label';
        agentLabel.textContent = `🤖 ${agentName}`;
        messageContent.appendChild(agentLabel);
    }
    
    // 如果有工具使用信息，显示工具列表
    if (type === 'assistant' && toolsUsed && toolsUsed.length > 0) {
        const toolsContainer = document.createElement('div');
        toolsContainer.className = 'tools-container';
        
        const toolsLabel = document.createElement('div');
        toolsLabel.className = 'tools-label';
        toolsLabel.innerHTML = '<i class="fas fa-tools"></i> 使用的工具:';
        
        const toolsDropdown = document.createElement('select');
        toolsDropdown.className = 'tools-dropdown';
        toolsDropdown.innerHTML = '<option value="">选择查看工具详情</option>';
        
        toolsUsed.forEach(tool => {
            const option = document.createElement('option');
            option.value = tool.name;
            option.textContent = `${tool.display_name} - ${tool.description}`;
            toolsDropdown.appendChild(option);
        });
        
        toolsContainer.appendChild(toolsLabel);
        toolsContainer.appendChild(toolsDropdown);
        messageContent.appendChild(toolsContainer);
    }
    
    // 文本或语音样式
    if (typeof content === 'string' && content.startsWith('__VOICE__:')) {
        const sec = parseInt(content.split(':')[1] || '0', 10) || 0;
        const voiceBox = document.createElement('div');
        voiceBox.className = 'voice-bubble';
        voiceBox.style.cssText = 'display:flex;align-items:center;gap:8px;background:#eef2ff;border:1px solid #dbe4ff;color:#3f51b5;padding:10px 12px;border-radius:10px;max-width:220px;';
        const icon = document.createElement('i');
        icon.className = 'fas fa-microphone';
        const label = document.createElement('span');
        label.textContent = `语音 ${sec}s`;
        voiceBox.appendChild(icon);
        voiceBox.appendChild(label);
        messageContent.appendChild(voiceBox);
    } else {
        const messageText = document.createElement('div');
        messageText.className = 'message-text';
        messageText.textContent = content;
        messageContent.appendChild(messageText);
    }
    
    // 如果有图片，显示图片
    if (type === 'assistant' && images && images.length > 0) {
        const imagesContainer = document.createElement('div');
        imagesContainer.className = 'images-container';
        
        images.forEach((image, index) => {
            const imgElement = document.createElement('img');
            imgElement.className = 'generated-image';
            imgElement.alt = image.alt || '生成的图像';
            // 将图片显示缩小：最大宽度480px，随容器自适应
            imgElement.style.maxWidth = '480px';
            imgElement.style.height = 'auto';
            imgElement.style.borderRadius = '8px';
            imgElement.style.marginTop = '10px';
            imgElement.style.cursor = 'pointer';
            
            if (image.type === 'url') {
                imgElement.src = image.data;
            } else if (image.type === 'base64') {
                imgElement.src = image.data;
            }
            
            // 添加点击放大功能
            imgElement.addEventListener('click', function() {
                showImageModal(image.data, image.alt);
            });
            
            imagesContainer.appendChild(imgElement);
        });
        
        messageContent.appendChild(imagesContainer);
    }
    
    const messageTime = document.createElement('div');
    messageTime.className = 'message-time';
    messageTime.textContent = new Date().toLocaleTimeString();
    
    messageContent.appendChild(messageTime);
    messageDiv.appendChild(avatar);
    messageDiv.appendChild(messageContent);
    
    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// 显示图片模态框
function showImageModal(imageSrc, imageAlt) {
    // 创建模态框
    const modal = document.createElement('div');
    modal.className = 'image-modal';
    modal.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background-color: rgba(0, 0, 0, 0.8);
        display: flex;
        justify-content: center;
        align-items: center;
        z-index: 1000;
        cursor: pointer;
    `;
    
    // 创建图片容器
    const imageContainer = document.createElement('div');
    imageContainer.style.cssText = `
        max-width: 90%;
        max-height: 90%;
        position: relative;
    `;
    
    // 创建图片
    const img = document.createElement('img');
    img.src = imageSrc;
    img.alt = imageAlt;
    img.style.cssText = `
        max-width: 100%;
        max-height: 100%;
        border-radius: 8px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
    `;
    
    // 创建关闭按钮
    const closeBtn = document.createElement('button');
    closeBtn.innerHTML = '×';
    closeBtn.style.cssText = `
        position: absolute;
        top: -40px;
        right: 0;
        background: none;
        border: none;
        color: white;
        font-size: 30px;
        cursor: pointer;
        padding: 5px 10px;
    `;
    
    // 添加事件监听器
    modal.addEventListener('click', function(e) {
        if (e.target === modal || e.target === closeBtn) {
            document.body.removeChild(modal);
        }
    });
    
    // 组装模态框
    imageContainer.appendChild(img);
    imageContainer.appendChild(closeBtn);
    modal.appendChild(imageContainer);
    document.body.appendChild(modal);
}

// 显示打字指示器
function showTypingIndicator() {
    const messagesContainer = document.getElementById('messagesContainer');
    const typingDiv = document.createElement('div');
    typingDiv.className = 'message assistant';
    typingDiv.id = 'typingIndicator';
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.innerHTML = '<i class="fas fa-robot"></i>';
    
    const typingContent = document.createElement('div');
    typingContent.className = 'typing-indicator';
    typingContent.innerHTML = `
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
    `;
    
    typingDiv.appendChild(avatar);
    typingDiv.appendChild(typingContent);
    messagesContainer.appendChild(typingDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// 隐藏打字指示器
function hideTypingIndicator() {
    const typingIndicator = document.getElementById('typingIndicator');
    if (typingIndicator) {
        typingIndicator.remove();
    }
}

// 处理键盘事件
function handleKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

// 自动调整输入框高度
function autoResize(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
}

// 这些功能已移除，智能体会自动调用工具

// 重试最后一条消息
function retryLastMessage() {
    if (lastUserMessage) {
        document.getElementById('messageInput').value = lastUserMessage;
        sendMessage();
    }
}

// 显示加载状态
function showLoading() {
    document.getElementById('loadingOverlay').style.display = 'flex';
    document.getElementById('sendBtn').disabled = true;
}

// 隐藏加载状态
function hideLoading() {
    document.getElementById('loadingOverlay').style.display = 'none';
    document.getElementById('sendBtn').disabled = false;
}

// 文件上传相关函数
function uploadFile() {
    document.getElementById('fileUploadModal').classList.add('show');
}

function closeFileUploadModal() {
    document.getElementById('fileUploadModal').classList.remove('show');
}

function handleDragOver(e) {
    e.preventDefault();
    e.currentTarget.style.borderColor = '#667eea';
    e.currentTarget.style.backgroundColor = '#f8f9ff';
}

function handleDrop(e) {
    e.preventDefault();
    e.currentTarget.style.borderColor = '#e0e0e0';
    e.currentTarget.style.backgroundColor = 'transparent';
    
    const files = e.dataTransfer.files;
    handleFiles(files);
}

function handleFileSelect(e) {
    const files = e.target.files;
    handleFiles(files);
}

async function handleFiles(files) {
    for (let file of files) {
        await uploadSingleFile(file);
    }
}

async function uploadSingleFile(file) {
    const formData = new FormData();
    formData.append('file', file);
    
    try {
        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (response.ok) {
            uploadedFiles.push({
                id: data.file_id,
                name: data.filename,
                size: file.size
            });
            updateUploadedFilesDisplay();
            showNotification('文件上传成功', 'success');

            // 如果是音频文件，自动触发一次会话（直达回答）
            const isAudioByType = (file.type || '').startsWith('audio/');
            const lower = (file.name || '').toLowerCase();
            const isAudioByExt = ['.mp3', '.wav', '.m4a', '.webm', '.ogg'].some(ext => lower.endsWith(ext));
            if (isAudioByType || isAudioByExt) {
                // 在对话中展示语音提示气泡
                addMessage('user', '__VOICE__:0');
                await autoSendWithFile(data.file_id, '');
            }
        } else {
            showNotification(`文件上传失败: ${data.error}`, 'error');
        }
    } catch (error) {
        showNotification(`文件上传失败: ${error.message}`, 'error');
    }
}

function updateUploadedFilesDisplay() {
    const container = document.getElementById('uploadedFiles');
    container.innerHTML = '';
    
    uploadedFiles.forEach((file, index) => {
        const fileItem = document.createElement('div');
        fileItem.className = 'file-item';
        const isAudio = file.isAudio;
        const iconHtml = isAudio ? '<i class="fas fa-microphone"></i>' : '<i class="fas fa-file"></i>';
        const extra = isAudio ? `<div class="file-size">时长：${file.duration || 0}s</div>` : `<div class="file-size">${formatFileSize(file.size)}</div>`;
        fileItem.innerHTML = `
            <div class="file-info">
                <div class="file-icon">${iconHtml}</div>
                <div class="file-details">
                    <div class="file-name">${file.name}</div>
                    ${extra}
                </div>
            </div>
            <div class="file-actions">
                <button class="file-action-btn delete" onclick="removeFile(${index})" title="删除文件">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        `;
        container.appendChild(fileItem);
    });
}

function removeFile(index) {
    const file = uploadedFiles[index];
    
    // 从服务器删除文件
    fetch(`/api/files/${file.id}`, {
        method: 'DELETE'
    }).catch(error => {
        console.error('删除文件失败:', error);
    });
    
    // 从本地列表删除
    uploadedFiles.splice(index, 1);
    updateUploadedFilesDisplay();
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// 对话历史相关函数
async function loadConversationHistory() {
    try {
        const response = await fetch('/api/conversations');
        const data = await response.json();
        
        if (response.ok) {
            displayConversationHistory(data.conversations);
        }
    } catch (error) {
        console.error('加载对话历史失败:', error);
    }
}

function displayConversationHistory(conversations) {
    const container = document.getElementById('conversationList');
    container.innerHTML = '';
    
    if (conversations.length === 0) {
        container.innerHTML = '<div style="text-align: center; color: #999; padding: 20px;">暂无对话历史</div>';
        return;
    }
    
    // 按时间分组
    const today = new Date().toDateString();
    const weekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
    
    const todayConversations = [];
    const weekConversations = [];
    const olderConversations = [];
    
    conversations.forEach(conv => {
        const convDate = new Date(conv.last_message_time);
        if (convDate.toDateString() === today) {
            todayConversations.push(conv);
        } else if (convDate > weekAgo) {
            weekConversations.push(conv);
        } else {
            olderConversations.push(conv);
        }
    });
    
    // 显示今天的对话
    if (todayConversations.length > 0) {
        addConversationGroup(container, '今天', todayConversations);
    }
    
    // 显示近7日的对话
    if (weekConversations.length > 0) {
        addConversationGroup(container, '近7日', weekConversations);
    }
    
    // 显示更早的对话
    if (olderConversations.length > 0) {
        addConversationGroup(container, '更早', olderConversations);
    }
}

function addConversationGroup(container, title, conversations) {
    const groupDiv = document.createElement('div');
    groupDiv.className = 'conversation-group';
    
    const groupTitle = document.createElement('div');
    groupTitle.className = 'conversation-group-title';
    groupTitle.textContent = title;
    groupTitle.style.cssText = 'font-size: 12px; color: #999; margin: 16px 0 8px; font-weight: 600;';
    
    groupDiv.appendChild(groupTitle);
    
    conversations.forEach(conv => {
        const convItem = document.createElement('div');
        convItem.className = 'conversation-item';
        if (conv.id === currentConversationId) {
            convItem.classList.add('active');
        }
        
        convItem.innerHTML = `
            <div class="conversation-title">${conv.title}</div>
            <div class="conversation-time">${formatTime(conv.last_message_time)}</div>
        `;
        
        convItem.addEventListener('click', () => loadConversation(conv.id));
        groupDiv.appendChild(convItem);
    });
    
    container.appendChild(groupDiv);
}

async function loadConversation(conversationId) {
    try {
        const response = await fetch(`/api/conversations/${conversationId}`);
        const data = await response.json();
        
        if (response.ok) {
            currentConversationId = conversationId;
            clearMessages();
            hideWelcomeScreen();
            
            data.messages.forEach(msg => {
                addMessage(msg.type, msg.message, msg.agent_name || null, msg.tools_used || [], msg.images || []);
            });
            
            // 更新对话历史显示
            loadConversationHistory();
        }
    } catch (error) {
        console.error('加载对话失败:', error);
    }
}

function clearAllConversations() {
    if (confirm('确定要清空所有对话历史吗？此操作不可恢复。')) {
        fetch('/api/clear', {
            method: 'POST'
        }).then(response => {
            if (response.ok) {
                startNewChat();
                loadConversationHistory();
                showNotification('所有对话已清空', 'success');
            }
        }).catch(error => {
            console.error('清空对话失败:', error);
            showNotification('清空对话失败', 'error');
        });
    }
}

function formatTime(timestamp) {
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now - date;
    
    if (diff < 60 * 1000) {
        return '刚刚';
    } else if (diff < 60 * 60 * 1000) {
        return Math.floor(diff / (60 * 1000)) + '分钟前';
    } else if (diff < 24 * 60 * 60 * 1000) {
        return Math.floor(diff / (60 * 60 * 1000)) + '小时前';
    } else {
        return date.toLocaleDateString();
    }
}

// 功能按钮处理
// 这些功能已移除，智能体会自动调用工具

// 通知系统
function showNotification(message, type = 'info') {
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.textContent = message;
    
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 12px 20px;
        border-radius: 8px;
        color: white;
        font-weight: 500;
        z-index: 3000;
        animation: slideIn 0.3s ease;
    `;
    
    switch (type) {
        case 'success':
            notification.style.backgroundColor = '#4caf50';
            break;
        case 'error':
            notification.style.backgroundColor = '#f44336';
            break;
        case 'warning':
            notification.style.backgroundColor = '#ff9800';
            break;
        default:
            notification.style.backgroundColor = '#2196f3';
    }
    
    document.body.appendChild(notification);
    
    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s ease';
        setTimeout(() => {
            document.body.removeChild(notification);
        }, 300);
    }, 3000);
}

// 添加CSS动画
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(100%);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
    
    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

// =========================
// MCP 管理（前端占位与API对接）
// =========================

let currentEditingMcpId = null;

function openMcpManager() {
    document.getElementById('mcpManagerModal').classList.add('show');
    loadMcpList();
}

function closeMcpManager() {
    document.getElementById('mcpManagerModal').classList.remove('show');
}

function openCreateMcp() {
    currentEditingMcpId = null;
    document.getElementById('mcpEditTitle').textContent = '新建 MCP';
    document.getElementById('mcpName').value = '';
    document.getElementById('mcpDesc').value = '';
    document.getElementById('mcpEnabled').checked = true;
    document.getElementById('mcpConfig').value = '';
    document.getElementById('mcpEditModal').classList.add('show');
}

function openEditMcp(mcp) {
    currentEditingMcpId = mcp.id;
    document.getElementById('mcpEditTitle').textContent = '编辑 MCP';
    document.getElementById('mcpName').value = mcp.name || '';
    document.getElementById('mcpDesc').value = mcp.description || '';
    document.getElementById('mcpEnabled').checked = !!mcp.enabled;
    document.getElementById('mcpConfig').value = mcp.config ? JSON.stringify(mcp.config, null, 2) : '';
    document.getElementById('mcpEditModal').classList.add('show');
}

function closeMcpEdit() {
    document.getElementById('mcpEditModal').classList.remove('show');
}

async function loadMcpList() {
    // 预留后端接口：GET /api/mcps -> { mcps: [{id,name,description,enabled,config}, ...] }
    try {
        const res = await fetch('/api/mcps');
        const data = await res.json();
        if (res.ok) {
            renderMcpList(data.mcps || []);
        } else {
            renderMcpList([]);
        }
    } catch (e) {
        console.error('加载MCP失败', e);
        renderMcpList([]);
    }
}

function renderMcpList(mcps) {
    const list = document.getElementById('mcpList');
    list.innerHTML = '';
    if (!mcps || mcps.length === 0) {
        list.innerHTML = '<div style="color:#999;">暂无MCP</div>';
        return;
    }

    mcps.forEach(mcp => {
        const item = document.createElement('div');
        item.className = 'mcp-item';
        item.style.cssText = 'display:flex; align-items:center; justify-content:space-between; padding:10px; border:1px solid #eee; border-radius:8px; margin-bottom:8px;';
        item.innerHTML = `
            <div style="display:flex; flex-direction:column; gap:4px;">
                <div style="font-weight:600;">${mcp.name || ''}</div>
                <div style="font-size:12px; color:#777;">${mcp.description || ''}</div>
                <div style="font-size:12px; color:#999;">ID: ${mcp.id || '-'}</div>
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
                <label style="display:flex; align-items:center; gap:6px; font-size:12px; color:#555;">
                    <input type="checkbox" ${mcp.enabled ? 'checked' : ''} onchange="toggleMcpEnabled('${mcp.id}', this.checked)">
                    启用
                </label>
                <button class="action-btn" title="编辑" onclick='openEditMcp(${JSON.stringify(mcp)})'><i class="fas fa-edit"></i></button>
                <button class="action-btn" title="删除" onclick="deleteMcp('${mcp.id}')"><i class="fas fa-trash"></i></button>
            </div>
        `;
        list.appendChild(item);
    });
}

async function saveMcp() {
    // 预留后端接口：
    // POST /api/mcps -> 创建；PUT /api/mcps/:id -> 更新
    const payload = {
        name: document.getElementById('mcpName').value.trim(),
        description: document.getElementById('mcpDesc').value.trim(),
        enabled: document.getElementById('mcpEnabled').checked,
        config: safeParseJson(document.getElementById('mcpConfig').value)
    };

    try {
        const url = currentEditingMcpId ? `/api/mcps/${currentEditingMcpId}` : '/api/mcps';
        const method = currentEditingMcpId ? 'PUT' : 'POST';
        const res = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            closeMcpEdit();
            loadMcpList();
            showNotification('保存成功', 'success');
        } else {
            showNotification('保存失败', 'error');
        }
    } catch (e) {
        showNotification('网络错误，保存失败', 'error');
    }
}

async function toggleMcpEnabled(id, enabled) {
    // 预留后端接口：PATCH /api/mcps/:id/enable {enabled}
    try {
        await fetch(`/api/mcps/${id}/enable`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled })
        });
        showNotification('状态已更新', 'success');
    } catch (e) {
        showNotification('更新失败', 'error');
    }
}

async function deleteMcp(id) {
    if (!confirm('确定删除该MCP吗？')) return;
    // 预留后端接口：DELETE /api/mcps/:id
    try {
        const res = await fetch(`/api/mcps/${id}`, { method: 'DELETE' });
        if (res.ok) {
            loadMcpList();
            showNotification('删除成功', 'success');
        } else {
            showNotification('删除失败', 'error');
        }
    } catch (e) {
        showNotification('网络错误，删除失败', 'error');
    }
}

function safeParseJson(text) {
    if (!text || !text.trim()) return null;
    try { return JSON.parse(text); } catch (e) { return null; }
}

// 通话相关函数
function startCall() {
    if (isInCall) {
        showNotification('您已经在通话中', 'warning');
        return;
    }
    
    // 跳转到通话页面
    window.location.href = '/call';
}

// 初始化Socket.IO连接
function initSocket() {
    if (socket) return socket;
    
    socket = io();
    
    socket.on('connect', function() {
        console.log('WebSocket连接成功');
    });
    
    socket.on('disconnect', function() {
        console.log('WebSocket连接断开');
    });
    
    socket.on('call_joined', function(data) {
        console.log('加入通话会话:', data.session_id);
    });
    
    socket.on('call_left', function(data) {
        console.log('离开通话会话');
    });
    
    socket.on('audio_received', function(data) {
        console.log('收到音频数据:', data.timestamp);
        // 这里可以添加音频播放逻辑
    });
    
    socket.on('audio_response', function(data) {
        console.log('收到音频响应:', data.timestamp);
        // 这里可以添加音频播放逻辑
    });
    
    return socket;
}
