


from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt
from typing import Dict
import asyncio
import json
import os

DATABASE_URL = "sqlite:///./idk.db"
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

os.makedirs("templates", exist_ok=True)

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

@app.get("/")
async def root():
    return {"message": "IDK Messenger API", "endpoints": ["/login", "/register", "/users", "/chat"]}

@app.get("/login")
async def login_page():
    with open("templates/login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/register")
async def register_page():
    with open("templates/register.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/users")
async def users_page():
    with open("templates/users.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/chat")
async def chat_page():
    with open("templates/chat.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
