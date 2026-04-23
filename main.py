
# main.py - исправленная версия

from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt
from typing import List, Optional, Dict
import asyncio
from collections import defaultdict
import json

DATABASE_URL = "sqlite:///./hail.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

class UserRegister(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class MessageSend(BaseModel):
    receiver_id: int
    content: str

class MessageOut(BaseModel):
    id: int
    sender_id: int
    receiver_id: int
    content: str
    timestamp: datetime

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_token(user_id: int, username: str):
    payload = {"user_id": user_id, "username": username, "exp": datetime.utcnow() + timedelta(days=7)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except:
        return None

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except:
                self.disconnect(user_id)

manager = ConnectionManager()

@app.post("/api/register")
def register(user: UserRegister, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    new_user = User(username=user.username, password=user.password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    token = create_token(new_user.id, new_user.username)
    return {"token": token, "user_id": new_user.id, "username": new_user.username}

@app.post("/api/login")
def login(user: UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == user.username, User.password == user.password).first()
    if not db_user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(db_user.id, db_user.username)
    return {"token": token, "user_id": db_user.id, "username": db_user.username}

@app.get("/api/users")
def get_users(token: str, db: Session = Depends(get_db)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    current_user_id = payload["user_id"]
    users = db.query(User).filter(User.id != current_user_id).all()
    return [{"id": u.id, "username": u.username} for u in users]

@app.get("/api/messages/{other_user_id}")
def get_messages(other_user_id: int, token: str, db: Session = Depends(get_db)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    current_user_id = payload["user_id"]
    messages = db.query(Message).filter(
        ((Message.sender_id == current_user_id) & (Message.receiver_id == other_user_id)) |
        ((Message.sender_id == other_user_id) & (Message.receiver_id == current_user_id))
    ).order_by(Message.timestamp).all()
    return [MessageOut(id=m.id, sender_id=m.sender_id, receiver_id=m.receiver_id, content=m.content, timestamp=m.timestamp) for m in messages]

@app.post("/api/messages")
def send_message(message: MessageSend, token: str, db: Session = Depends(get_db)):
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    current_user_id = payload["user_id"]
    receiver = db.query(User).filter(User.id == message.receiver_id).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    new_message = Message(sender_id=current_user_id, receiver_id=message.receiver_id, content=message.content)
    db.add(new_message)
    db.commit()
    db.refresh(new_message)
    message_data = {
        "type": "message",
        "id": new_message.id,
        "sender_id": new_message.sender_id,
        "receiver_id": new_message.receiver_id,
        "content": new_message.content,
        "timestamp": new_message.timestamp.isoformat()
    }
    asyncio.create_task(manager.send_personal_message(message_data, message.receiver_id))
    return MessageOut(id=new_message.id, sender_id=new_message.sender_id, receiver_id=new_message.receiver_id, content=new_message.content, timestamp=new_message.timestamp)

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    payload = verify_token(token)
    if not payload:
        await websocket.close(code=1008)
        return
    user_id = payload["user_id"]
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message_data = json.loads(data)
                if message_data.get("type") == "typing":
                    receiver_id = message_data.get("receiver_id")
                    if receiver_id:
                        await manager.send_personal_message({"type": "typing", "sender_id": user_id}, receiver_id)
            except:
                pass
    except WebSocketDisconnect:
        manager.disconnect(user_id)

html_mess = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hail - Мессенджер</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #181818; color: #ffffff; }
        .messenger { width: 100%; min-height: 100vh; display: flex; flex-direction: column; background-color: #202020; }
        .header { background-color: #111111; height: 60px; flex-shrink: 0; }
        .search-container { display: flex; justify-content: center; padding: 20px 40px 24px; }
        .search-box { display: flex; align-items: center; background-color: #2c2c2c; border-radius: 24px; padding: 12px 24px; width: 100%; max-width: 600px; gap: 12px; border: 1px solid #3a3a3a; }
        .search-box svg { stroke: #a0a0a0; }
        .search-box input { border: none; background: transparent; outline: none; font-size: 16px; color: #e0e0e0; width: 100%; }
        .search-box input::placeholder { color: #707070; }
        .chat-list { flex: 1; padding: 0 40px 20px; display: flex; flex-direction: column; gap: 16px; max-width: 900px; margin: 0 auto; width: 100%; }
        .chat-item { display: flex; align-items: center; background-color: #383838; border-radius: 16px; padding: 18px 24px; gap: 18px; cursor: pointer; transition: background-color 0.2s; border: 1px solid #444; }
        .chat-item:hover { background-color: #454545; }
        .avatar { width: 56px; height: 56px; border-radius: 14px; background-color: #555555; flex-shrink: 0; display: flex; align-items: center; justify-content: center; }
        .avatar svg { opacity: 0.9; fill: #cccccc; width: 32px; height: 32px; }
        .chat-info { flex: 1; }
        .chat-name { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
        .bottom-bar { display: flex; justify-content: center; gap: 60px; align-items: center; padding: 24px 0 16px; flex-shrink: 0; background-color: #202020; }
        .bottom-bar .icon-btn { background: none; border: none; cursor: pointer; padding: 12px; display: flex; align-items: center; justify-content: center; border-radius: 12px; transition: background-color 0.2s; }
        .bottom-bar .icon-btn:hover { background-color: #333333; }
        .bottom-bar .icon-btn svg { stroke: #ffffff; width: 30px; height: 30px; }
        .footer { background-color: #111111; height: 50px; flex-shrink: 0; }
        @media (max-width: 768px) { .search-container { padding: 16px 20px 20px; } .chat-list { padding: 0 20px 20px; } .chat-item { padding: 14px 18px; } .avatar { width: 48px; height: 48px; } .bottom-bar { gap: 40px; } }
        .logout-btn { background: none; border: none; color: #ff4444; cursor: pointer; font-size: 14px; margin-top: 4px; }
        .no-users { text-align: center; padding: 40px; color: #888; }
    </style>
</head>
<body>
<div class="messenger">
    <div class="header"></div>
    <div class="search-container">
        <div class="search-box">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" id="searchInput" placeholder="Поиск">
        </div>
    </div>
    <div class="chat-list" id="chatList"></div>
    <div class="bottom-bar">
        <button class="icon-btn" id="logoutBtn" aria-label="Выйти">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        </button>
        <button class="icon-btn" id="profileBtn" aria-label="Профиль">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        </button>
    </div>
    <div class="footer"></div>
</div>
<script>
    const token = localStorage.getItem('token');
    const userId = localStorage.getItem('userId');
    if (!token || !userId) { window.location.href = '/login'; }
    let ws = null;
    let currentChatUser = null;

    function connectWebSocket() {
        ws = new WebSocket(`ws://localhost:8000/ws/${token}`);
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'message' && currentChatUser && data.sender_id === currentChatUser) {
                addMessageToChat(data);
            } else if (data.type === 'message' && currentChatUser === null) {
                loadUsers();
            }
        };
        ws.onclose = () => setTimeout(connectWebSocket, 3000);
    }
    connectWebSocket();

    async function loadUsers() {
        const resp = await fetch(`/api/users?token=${token}`);
        if (resp.status === 401) { localStorage.clear(); window.location.href = '/login'; return; }
        const users = await resp.json();
        const searchTerm = document.getElementById('searchInput').value.toLowerCase();
        const filtered = users.filter(u => u.username.toLowerCase().includes(searchTerm));
        const container = document.getElementById('chatList');
        if (filtered.length === 0) { container.innerHTML = '<div class="no-users">Нет пользователей</div>'; return; }
        container.innerHTML = filtered.map(u => `
            <div class="chat-item" onclick="openChat(${u.id}, '${u.username}')">
                <div class="avatar"><svg viewBox="0 0 24 24"><path d="M12 12c2.7 0 5-2.3 5-5s-2.3-5-5-5-5 2.3-5 5 2.3 5 5 5zm0 2c-3.3 0-10 1.7-10 5v3h20v-3c0-3.3-6.7-5-10-5z"/></svg></div>
                <div class="chat-info"><div class="chat-name">${escapeHtml(u.username)}</div></div>
            </div>
        `).join('');
    }

    function escapeHtml(str) { return str.replace(/[&<>]/g, function(m){if(m==='&') return '&amp;'; if(m==='<') return '&lt;'; if(m==='>') return '&gt;'; return m;}); }

    window.openChat = async function(otherId, username) {
        currentChatUser = otherId;
        const resp = await fetch(`/api/messages/${otherId}?token=${token}`);
        const messages = await resp.json();
        const chatHtml = `
            <div style="position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #1a1a1a; z-index: 1000; display: flex; flex-direction: column;">
                <div style="background: #2d2d2d; padding: 15px 20px; display: flex; align-items: center; gap: 15px;">
                    <button onclick="closeChat()" style="background: none; border: none; color: white; font-size: 24px; cursor: pointer;">&lt;</button>
                    <div style="font-size: 18px; font-weight: 600;">${escapeHtml(username)}</div>
                </div>
                <div id="chatMessages" style="flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 10px;"></div>
                <div style="background: #2d2d2d; padding: 15px 20px; display: flex; gap: 12px;">
                    <input type="text" id="messageInput" placeholder="Сообщение" style="flex: 1; background: #3d3d3d; border: none; border-radius: 25px; padding: 12px 20px; color: white; outline: none;">
                    <button onclick="sendMessage()" style="background: #8b5cf6; border: none; border-radius: 25px; padding: 12px 24px; color: white; cursor: pointer;">Отправить</button>
                </div>
            </div>
        `;
        document.body.innerHTML = chatHtml;
        const messagesContainer = document.getElementById('chatMessages');
        messages.forEach(msg => {
            const div = document.createElement('div');
            div.className = `message ${msg.sender_id == userId ? 'sent' : 'received'}`;
            div.innerHTML = `<div class="message-text">${escapeHtml(msg.content)}</div><div class="message-time">${new Date(msg.timestamp).toLocaleTimeString()}</div>`;
            messagesContainer.appendChild(div);
        });
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
        document.getElementById('messageInput').addEventListener('keypress', (e) => { if(e.key === 'Enter') sendMessage(); });
    }

    window.closeChat = function() { window.location.reload(); }

    window.sendMessage = async function() {
        const input = document.getElementById('messageInput');
        const content = input.value.trim();
        if(!content) return;
        const resp = await fetch(`/api/messages?token=${token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receiver_id: currentChatUser, content: content })
        });
        if(resp.ok) {
            const msg = await resp.json();
            addMessageToChat(msg);
            input.value = '';
        }
    }

    function addMessageToChat(msg) {
        const container = document.getElementById('chatMessages');
        if(!container) return;
        const div = document.createElement('div');
        div.className = `message ${msg.sender_id == userId ? 'sent' : 'received'}`;
        div.innerHTML = `<div class="message-text">${escapeHtml(msg.content)}</div><div class="message-time">${new Date(msg.timestamp).toLocaleTimeString()}</div>`;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    document.getElementById('searchInput').addEventListener('input', loadUsers);
    document.getElementById('logoutBtn').addEventListener('click', () => { localStorage.clear(); window.location.href = '/login'; });
    document.getElementById('profileBtn').addEventListener('click', () => { alert(`ID: ${userId}\\nТокен: ${token.substring(0,20)}...`); });
    loadUsers();
</script>
<style>
    .message { max-width: 70%; padding: 12px 16px; border-radius: 20px; position: relative; word-wrap: break-word; }
    .message.received { background-color: #3d3d3d; align-self: flex-start; border-bottom-left-radius: 5px; }
    .message.sent { background-color: #8b5cf6; align-self: flex-end; border-bottom-right-radius: 5px; }
    .message-text { font-size: 16px; line-height: 1.4; margin-bottom: 4px; }
    .message-time { font-size: 12px; opacity: 0.7; text-align: right; }
</style>
</body>
</html>
'''

html_login = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход - Hail</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #1a1a1a; color: #ffffff; height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        .login-form { width: 100%; max-width: 400px; padding: 40px 30px; display: flex; flex-direction: column; gap: 20px; }
        .app-title { text-align: center; font-size: 36px; font-weight: 700; color: #8b5cf6; margin-bottom: 20px; }
        .input-group { background-color: #2d2d2d; border-radius: 30px; display: flex; align-items: center; padding: 0 20px; transition: all 0.3s; }
        .input-group:focus-within { background-color: #3a3a3a; box-shadow: 0 0 0 2px #8b5cf6; }
        .input-group svg { width: 24px; height: 24px; flex-shrink: 0; fill: #888; }
        .input-group input { flex: 1; background: transparent; border: none; padding: 18px 15px; color: #ffffff; font-size: 18px; outline: none; }
        .input-group input::placeholder { color: #888; }
        .login-button { background: none; border: none; color: #00bfff; font-size: 18px; font-weight: 500; padding: 15px; cursor: pointer; text-align: center; transition: color 0.3s; }
        .login-button:hover { color: #33ccff; }
        .divider { display: flex; align-items: center; gap: 15px; margin: 10px 0; }
        .divider::before, .divider::after { content: ''; flex: 1; height: 1px; background-color: #3d3d3d; }
        .divider span { color: #666; font-size: 14px; }
        .links { text-align: center; display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
        .links a { color: #8b5cf6; text-decoration: none; font-size: 15px; cursor: pointer; }
        .links a:hover { text-decoration: underline; }
        .switch-link { color: #8b5cf6; cursor: pointer; }
    </style>
</head>
<body>
    <div class="login-form" id="loginForm">
        <div class="app-title">Hail</div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
            <input type="text" id="username" placeholder="Username">
        </div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></svg>
            <input type="password" id="password" placeholder="Пароль">
        </div>
        <button class="login-button" id="loginBtn">Войти</button>
        <div class="divider"><span>или</span></div>
        <div class="links"><a id="showRegister">Создать аккаунт</a></div>
    </div>
    <div class="login-form" id="registerForm" style="display: none;">
        <div class="app-title">Hail</div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
            <input type="text" id="regUsername" placeholder="Username">
        </div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></svg>
            <input type="password" id="regPassword" placeholder="Пароль">
        </div>
        <button class="login-button" id="registerBtn">Зарегистрироваться</button>
        <div class="divider"><span>или</span></div>
        <div class="links"><a id="showLogin">Уже есть аккаунт? Войти</a></div>
    </div>
    <script>
        document.getElementById('loginBtn').onclick = async () => {
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const resp = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
            if(resp.ok) { const data = await resp.json(); localStorage.setItem('token', data.token); localStorage.setItem('userId', data.user_id); window.location.href = '/'; }
            else { const error = await resp.json(); alert('Ошибка входа: ' + (error.detail || 'неверные данные')); }
        };
        document.getElementById('registerBtn').onclick = async () => {
            const username = document.getElementById('regUsername').value;
            const password = document.getElementById('regPassword').value;
            const resp = await fetch('/api/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
            if(resp.ok) { const data = await resp.json(); localStorage.setItem('token', data.token); localStorage.setItem('userId', data.user_id); window.location.href = '/'; }
            else { const error = await resp.json(); alert('Ошибка регистрации: ' + (error.detail || 'пользователь уже существует')); }
        };
        document.getElementById('showRegister').onclick = () => { document.getElementById('loginForm').style.display = 'none'; document.getElementById('registerForm').style.display = 'flex'; };
        document.getElementById('showLogin').onclick = () => { document.getElementById('registerForm').style.display = 'none'; document.getElementById('loginForm').style.display = 'flex'; };
        document.addEventListener('keypress', (e) => { if(e.key === 'Enter') { if(document.getElementById('loginForm').style.display !== 'none') document.getElementById('loginBtn').click(); else document.getElementById('registerBtn').click(); } });
    </script>
</body>
</html>
'''

@app.get("/")
async def root():
    return HTMLResponse(html_mess)

@app.get("/login")
async def login_page():
    return HTMLResponse(html_login)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
