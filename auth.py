from fastapi import APIRouter, HTTPException, Depends, status, Request, Body, UploadFile, File
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from pymongo import MongoClient
from jose import jwt, JWTError
from passlib.context import CryptContext
from datetime import datetime, timedelta
import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
from bson import ObjectId

router = APIRouter()

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

MONGO_URI = os.getenv("MONGO_URI")
JWT_SECRET = os.getenv("JWT_SECRET", "secret")
JWT_ALGORITHM = "HS256"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

client = MongoClient(MONGO_URI)
db = client["sch"]
users = db["users"]

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    department: str
    year: int = 1
    bio: Optional[str] = ""

class UserOut(BaseModel):
    id: str = Field(..., alias="_id")
    name: str
    email: EmailStr
    department: str
    year: int
    bio: Optional[str] = ""

class Token(BaseModel):
    access_token: str
    token_type: str

class UserUpdate(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None
    department: Optional[str] = None
    year: Optional[int] = None
    profilePicture: Optional[str] = None

class LoginRequest(BaseModel):
    email: str
    password: str

def get_user_by_email(email: str):
    return users.find_one({"email": email})

def get_user_by_id(user_id: str):
    # Ensure user_id is an ObjectId
    try:
        obj_id = ObjectId(user_id)
    except Exception:
        obj_id = user_id
    return users.find_one({"_id": obj_id})

def authenticate_user(email: str, password: str):
    user = get_user_by_email(email)
    if not user:
        return False
    if not pwd_context.verify(password, user["password"]):
        return False
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=24))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def get_current_user_sync(token: str = Depends(oauth2_scheme)):
    print("Token received:", token)  # Debug print
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = get_user_by_id(user_id)
    if user is None:
        raise credentials_exception
    return user

@router.post("/register", response_model=UserOut)
def register(user: UserCreate):
    if get_user_by_email(user.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    hashed_pw = pwd_context.hash(user.password)
    user_dict = user.dict()
    user_dict["password"] = hashed_pw
    res = users.insert_one(user_dict)
    user_dict["_id"] = str(res.inserted_id)
    del user_dict["password"]
    return user_dict

@router.post("/login", response_model=Token)
def login(request: LoginRequest):
    if not request.email or not request.password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    user = authenticate_user(request.email, request.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    access_token = create_access_token({"user_id": str(user["_id"])})
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/me", response_model=UserOut)
def me(current_user: dict = Depends(get_current_user_sync)):
    current_user["_id"] = str(current_user["_id"])
    del current_user["password"]
    return current_user

@router.put("/profile", response_model=UserOut)
def update_profile(
    update: UserUpdate,
    current_user: dict = Depends(get_current_user_sync)
):
    updates = {k: v for k, v in update.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    users.update_one({"_id": current_user["_id"]}, {"$set": updates})
    user = get_user_by_id(current_user["_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user["_id"] = str(user["_id"])
    del user["password"]
    return user

@router.post("/logout")
def logout(current_user: dict = Depends(get_current_user_sync)):
    users.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"isOnline": False, "lastSeen": datetime.utcnow()}}
    )
    return {"message": "Logged out successfully"}

class EmailRequest(BaseModel):
    email: EmailStr

@router.post("/forgot-password")
def forgot_password(req: EmailRequest):
    user = get_user_by_email(req.email)
    if not user:
        return {"message": "If this email is registered, a reset link has been sent."}
    reset_token = create_access_token({"user_id": str(user["_id"]), "reset": True}, expires_delta=timedelta(hours=1))
    reset_url = f"http://localhost:5173/reset-password/{reset_token}"
    message = EmailMessage()
    message["From"] = EMAIL_USER
    message["To"] = user["email"]
    message["Subject"] = "Reset your Student Hub password"
    message.set_content(f"Click the link to reset your password: {reset_url}")
    if EMAIL_USER is None or EMAIL_PASS is None:
        print("Error sending email: EMAIL_USER or EMAIL_PASS is not set")
        # Optionally, raise HTTPException(status_code=500, detail="Email credentials not configured")
    else:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(str(EMAIL_USER), str(EMAIL_PASS))
                server.send_message(message)
    return {"message": "If this email is registered, a reset link has been sent."}

class ResetPasswordRequest(BaseModel):
    password: str

@router.post("/reset-password/{token}")
def reset_password(token: str, req: ResetPasswordRequest):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if not payload.get("reset"):
            raise HTTPException(status_code=400, detail="Invalid token")
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=400, detail="Invalid token")
        user = get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid token")
        hashed_pw = pwd_context.hash(req.password)
        users.update_one({"_id": user["_id"]}, {"$set": {"password": hashed_pw}})
        return {"message": "Password reset successful"}
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

@router.post("/profile-picture", response_model=UserOut)
def upload_profile_picture(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user_sync)
):
    import cloudinary.uploader
    result = cloudinary.uploader.upload(file.file, resource_type="image", filename=file.filename)
    url = result["secure_url"]
    users.update_one({"_id": current_user["_id"]}, {"$set": {"profile_picture": url}})
    user = get_user_by_id(current_user["_id"])
    if user:
        user["_id"] = str(user["_id"])
        if "password" in user:
            del user["password"]
        return user
    else:
        raise HTTPException(status_code=404, detail="User not found") 