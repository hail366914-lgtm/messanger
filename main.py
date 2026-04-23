
# main.py
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt
from typing import Dict
import asyncio
import json

DATABASE_URL = "sqlite:///./hail.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

SECRET_KEY = "secret-key-change-in-production"
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

class UserRegister(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class MessageSend(BaseModel):
    receiver_id: int
    content: str

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
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
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
    return [{"id": m.id, "sender_id": m.sender_id, "receiver_id": m.receiver_id, "content": m.content, "timestamp": m.timestamp.isoformat()} for m in messages]

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
    return message_data

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

MAIN_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hail - Мессенджер</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #181818; color: #ffffff; }
        .container { width: 100%; min-height: 100vh; background-color: #202020; }
        .header { background-color: #111111; height: 60px; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; }
        .header h1 { font-size: 20px; color: #8b5cf6; }
        .logout-btn { background: #333; border: none; color: #ff4444; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 14px; }
        .search-container { padding: 20px 40px; }
        .search-box { display: flex; align-items: center; background-color: #2c2c2c; border-radius: 24px; padding: 12px 24px; max-width: 600px; margin: 0 auto; gap: 12px; }
        .search-box input { flex: 1; background: transparent; border: none; outline: none; font-size: 16px; color: #e0e0e0; }
        .search-box input::placeholder { color: #707070; }
        .users-list { padding: 0 40px 20px; max-width: 900px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
        .user-card { display: flex; align-items: center; background-color: #383838; border-radius: 16px; padding: 18px 24px; gap: 18px; cursor: pointer; transition: background-color 0.2s; border: 1px solid #444; }
        .user-card:hover { background-color: #454545; }
        .avatar { width: 56px; height: 56px; border-radius: 14px; background-color: #555555; display: flex; align-items: center; justify-content: center; }
        .avatar svg { width: 32px; height: 32px; fill: #cccccc; }
        .user-info { flex: 1; }
        .user-name { font-size: 18px; font-weight: 600; }
        .empty { text-align: center; padding: 40px; color: #888; }
        .chat-window { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #1a1a1a; z-index: 1000; display: flex; flex-direction: column; }
        .chat-header { background: #2d2d2d; padding: 15px 20px; display: flex; align-items: center; gap: 15px; }
        .chat-back { background: none; border: none; color: white; font-size: 24px; cursor: pointer; }
        .chat-title { font-size: 18px; font-weight: 600; flex: 1; }
        .chat-messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 10px; }
        .message { max-width: 70%; padding: 12px 16px; border-radius: 20px; word-wrap: break-word; }
        .message.sent { background-color: #8b5cf6; align-self: flex-end; border-bottom-right-radius: 5px; }
        .message.received { background-color: #3d3d3d; align-self: flex-start; border-bottom-left-radius: 5px; }
        .message-text { font-size: 16px; margin-bottom: 4px; }
        .message-time { font-size: 11px; opacity: 0.7; text-align: right; }
        .chat-input { background: #2d2d2d; padding: 15px 20px; display: flex; gap: 12px; }
        .chat-input input { flex: 1; background: #3d3d3d; border: none; border-radius: 25px; padding: 12px 20px; color: white; outline: none; font-size: 16px; }
        .chat-input button { background: #8b5cf6; border: none; border-radius: 25px; padding: 12px 24px; color: white; cursor: pointer; font-size: 16px; }
        .typing { font-size: 12px; color: #888; padding: 5px 20px; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Hail</h1>
        <button class="logout-btn" onclick="logout()">Выйти</button>
    </div>
    <div class="search-container">
        <div class="search-box">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#a0a0a0" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" id="searchInput" placeholder="Поиск пользователей...">
        </div>
    </div>
    <div class="users-list" id="usersList"></div>
</div>

<script>
    const token = localStorage.getItem('token');
    const userId = localStorage.getItem('userId');
    
    if (!token || !userId) {
        window.location.href = '/login';
    }
    
    let ws = null;
    let currentChatUser = null;
    
    function connectWebSocket() {
        ws = new WebSocket(`ws://localhost:8000/ws/${token}`);
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'message' && currentChatUser && data.sender_id === currentChatUser) {
                addMessageToChat(data);
            }
        };
        ws.onclose = () => {
            setTimeout(connectWebSocket, 3000);
        };
    }
    connectWebSocket();
    
    async function loadUsers() {
        try {
            const resp = await fetch(`/api/users?token=${token}`);
            if (resp.status === 401) {
                localStorage.clear();
                window.location.href = '/login';
                return;
            }
            const users = await resp.json();
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            const filtered = users.filter(u => u.username.toLowerCase().includes(searchTerm));
            const container = document.getElementById('usersList');
            
            if (filtered.length === 0) {
                container.innerHTML = '<div class="empty">Нет пользователей</div>';
                return;
            }
            
            container.innerHTML = filtered.map(u => `
                <div class="user-card" onclick="openChat(${u.id}, '${escapeHtml(u.username)}')">
                    <div class="avatar">
                        <svg viewBox="0 0 24 24"><path d="M12 12c2.7 0 5-2.3 5-5s-2.3-5-5-5-5 2.3-5 5 2.3 5 5 5zm0 2c-3.3 0-10 1.7-10 5v3h20v-3c0-3.3-6.7-5-10-5z"/></svg>
                    </div>
                    <div class="user-info">
                        <div class="user-name">${escapeHtml(u.username)}</div>
                    </div>
                </div>
            `).join('');
        } catch(e) {
            console.error(e);
        }
    }
    
    function escapeHtml(str) {
        return str.replace(/[&<>]/g, function(m) {
            if (m === '&') return '&amp;';
            if (m === '<') return '&lt;';
            if (m === '>') return '&gt;';
            return m;
        });
    }
    
    async function openChat(otherId, username) {
        currentChatUser = otherId;
        
        const resp = await fetch(`/api/messages/${otherId}?token=${token}`);
        const messages = await resp.json();
        
        const chatHtml = `
            <div class="chat-window" id="chatWindow">
                <div class="chat-header">
                    <button class="chat-back" onclick="closeChat()">&lt;</button>
                    <div class="chat-title">${escapeHtml(username)}</div>
                </div>
                <div class="chat-messages" id="chatMessages"></div>
                <div class="chat-input">
                    <input type="text" id="messageInput" placeholder="Сообщение..." onkeypress="if(event.key==='Enter') sendMessage()">
                    <button onclick="sendMessage()">Отправить</button>
                </div>
            </div>
        `;
        
        document.body.innerHTML = chatHtml;
        
        const messagesContainer = document.getElementById('chatMessages');
        for (const msg of messages) {
            const div = document.createElement('div');
            div.className = `message ${msg.sender_id == userId ? 'sent' : 'received'}`;
            div.innerHTML = `<div class="message-text">${escapeHtml(msg.content)}</div><div class="message-time">${new Date(msg.timestamp).toLocaleTimeString()}</div>`;
            messagesContainer.appendChild(div);
        }
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
    
    function closeChat() {
        currentChatUser = null;
        window.location.reload();
    }
    
    async function sendMessage() {
        const input = document.getElementById('messageInput');
        const content = input.value.trim();
        if (!content) return;
        
        const resp = await fetch(`/api/messages?token=${token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receiver_id: currentChatUser, content: content })
        });
        
        if (resp.ok) {
            const msg = await resp.json();
            addMessageToChat(msg);
            input.value = '';
        }
    }
    
    function addMessageToChat(msg) {
        const container = document.getElementById('chatMessages');
        if (!container) return;
        
        const div = document.createElement('div');
        div.className = `message ${msg.sender_id == userId ? 'sent' : 'received'}`;
        div.innerHTML = `<div class="message-text">${escapeHtml(msg.content)}</div><div class="message-time">${new Date(msg.timestamp).toLocaleTimeString()}</div>`;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }
    
    function logout() {
        localStorage.clear();
        window.location.href = '/login';
    }
    
    document.getElementById('searchInput').addEventListener('input', loadUsers);
    loadUsers();
</script>
</body>
</html>
"""

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hail - Вход</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #1a1a1a; color: #ffffff; height: 100vh; display: flex; align-items: center; justify-content: center; }
        .form-container { width: 100%; max-width: 400px; padding: 40px 30px; }
        .title { text-align: center; font-size: 42px; font-weight: 700; color: #8b5cf6; margin-bottom: 40px; }
        .input-group { background-color: #2d2d2d; border-radius: 30px; display: flex; align-items: center; padding: 0 20px; margin-bottom: 20px; transition: box-shadow 0.3s; }
        .input-group:focus-within { box-shadow: 0 0 0 2px #8b5cf6; }
        .input-group svg { width: 24px; height: 24px; fill: #888; flex-shrink: 0; }
        .input-group input { flex: 1; background: transparent; border: none; padding: 18px 15px; color: #ffffff; font-size: 18px; outline: none; }
        .input-group input::placeholder { color: #888; }
        .btn { width: 100%; background: none; border: none; color: #00bfff; font-size: 18px; font-weight: 500; padding: 15px; cursor: pointer; transition: opacity 0.3s; }
        .btn:hover { opacity: 0.7; }
        .divider { display: flex; align-items: center; gap: 15px; margin: 20px 0; }
        .divider::before, .divider::after { content: ''; flex: 1; height: 1px; background-color: #3d3d3d; }
        .divider span { color: #666; font-size: 14px; }
        .link { text-align: center; color: #8b5cf6; text-decoration: none; cursor: pointer; display: block; margin-top: 10px; }
        .link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div id="loginForm" class="form-container">
        <div class="title">Hail</div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
            <input type="text" id="loginUsername" placeholder="Username">
        </div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></svg>
            <input type="password" id="loginPassword" placeholder="Пароль">
        </div>
        <button class="btn" onclick="login()">Войти</button>
        <div class="divider"><span>или</span></div>
        <a class="link" onclick="showRegister()">Создать аккаунт</a>
    </div>

    <div id="registerForm" class="form-container" style="display: none;">
        <div class="title">Hail</div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
            <input type="text" id="regUsername" placeholder="Username">
        </div>
        <div class="input-group">
            <svg viewBox="0 0 24 24"><path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/></svg>
            <input type="password" id="regPassword" placeholder="Пароль">
        </div>
        <button class="btn" onclick="register()">Зарегистрироваться</button>
        <div class="divider"><span>или</span></div>
        <a class="link" onclick="showLogin()">Уже есть аккаунт? Войти</a>
    </div>

    <script>
        async function login() {
            const username = document.getElementById('loginUsername').value;
            const password = document.getElementById('loginPassword').value;
            if (!username || !password) {
                alert('Заполните все поля');
                return;
            }
            const resp = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            if (resp.ok) {
                const data = await resp.json();
                localStorage.setItem('token', data.token);
                localStorage.setItem('userId', data.user_id);
                window.location.href = '/';
            } else {
                const error = await resp.json();
                alert('Ошибка: ' + (error.detail || 'Неверные данные'));
            }
        }

        async function register() {
            const username = document.getElementById('regUsername').value;
            const password = document.getElementById('regPassword').value;
            if (!username || !password) {
                alert('Заполните все поля');
                return;
            }
            const resp = await fetch('/api/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            if (resp.ok) {
                const data = await resp.json();
                localStorage.setItem('token', data.token);
                localStorage.setItem('userId', data.user_id);
                window.location.href = '/';
            } else {
                const error = await resp.json();
                alert('Ошибка: ' + (error.detail || 'Пользователь уже существует'));
            }
        }

        function showRegister() {
            document.getElementById('loginForm').style.display = 'none';
            document.getElementById('registerForm').style.display = 'block';
        }

        function showLogin() {
            document.getElementById('registerForm').style.display = 'none';
            document.getElementById('loginForm').style.display = 'block';
        }

        document.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                if (document.getElementById('loginForm').style.display !== 'none') {
                    login();
                } else {
                    register();
                }
            }
        });
    </script>
</body>
</html>
"""

@app.get("/")
async def root():
    return HTMLResponse(MAIN_PAGE)

@app.get("/login")
async def login_page():
    return HTMLResponse(LOGIN_PAGE)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
