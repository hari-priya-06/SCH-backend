import os
from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Body, status, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from auth import router as auth_router, get_current_user_sync
from typing import List, Optional
from pydantic import BaseModel, Field
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import cloudinary
import cloudinary.uploader

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

# Cloudinary config
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

app = FastAPI()

origins = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client[os.getenv("MONGO_DB_NAME", "sch")]
posts = db["posts"]
users = db["users"]

# Pydantic models
class PostCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    category: str
    tags: Optional[List[str]] = []
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    original_name: Optional[str] = None

class PostOut(BaseModel):
    id: str = Field(..., alias="_id")
    user_id: str
    title: str
    description: Optional[str] = ""
    category: str
    tags: List[str] = []
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    original_name: Optional[str] = None
    created_at: datetime

# Helper to convert ObjectId
def oid(obj):
    if isinstance(obj, ObjectId):
        return str(obj)
    return obj

def post_to_dict(post):
    post["_id"] = oid(post["_id"])
    post["user_id"] = oid(post["user_id"])
    # Ensure likes is a list of strings
    if "likes" in post:
        post["likes"] = [oid(uid) for uid in post["likes"]]
    # Ensure comments user_id is string and created_at is isoformat
    if "comments" in post:
        for comment in post["comments"]:
            if "user_id" in comment:
                comment["user_id"] = oid(comment["user_id"])
            if "created_at" in comment and hasattr(comment["created_at"], 'isoformat'):
                comment["created_at"] = comment["created_at"].isoformat()
    # Add user info only if not present
    if not post.get("user_name") or not post.get("user_email"):
        user = users.find_one({"_id": ObjectId(post["user_id"])});
        if user:
            post["user_name"] = user.get("name", "Unknown")
            post["user_email"] = user.get("email", "")
            post["user_department"] = user.get("department", "")
        else:
            post["user_name"] = "Unknown"
            post["user_email"] = ""
            post["user_department"] = ""
    return post

@app.post("/api/posts", response_model=PostOut)
def create_post(
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    tags: str = Form(""),
    file: UploadFile = File(None),
    current_user: dict = Depends(get_current_user_sync)
):
    file_url = None
    file_type = None
    original_name = None
    if file:
        # Determine resource_type based on file content type
        if file.content_type == "application/pdf":
            result = cloudinary.uploader.upload(file.file, resource_type="raw", filename=file.filename)
            file_type = "pdf"
        elif file.content_type and file.content_type.startswith("image/"):
            result = cloudinary.uploader.upload(file.file, resource_type="image", filename=file.filename)
            file_type = file.content_type.split("/")[1]
        else:
            result = cloudinary.uploader.upload(file.file, resource_type="auto", filename=file.filename)
            file_type = file.content_type if file.content_type else None
        file_url = result["secure_url"]
        original_name = file.filename
    post_doc = {
        "user_id": ObjectId(current_user["_id"]),
        "title": title,
        "description": description,
        "category": category,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "file_url": file_url,
        "file_type": file_type,
        "original_name": original_name,
        "created_at": datetime.utcnow(),
        "likes": [],  # list of user IDs
        "comments": [],  # list of comment objects
        "user_name": current_user.get("name", "Unknown"),
        "user_email": current_user.get("email", "")
    }
    res = posts.insert_one(post_doc)
    post_doc["_id"] = res.inserted_id
    return post_to_dict(post_doc)

@app.get("/api/posts", response_model=List[PostOut])
def list_posts():
    return [post_to_dict(post) for post in posts.find().sort("created_at", -1)]

@app.get("/api/posts/user/{user_id}", response_model=List[PostOut])
def list_user_posts(user_id: str):
    return [post_to_dict(post) for post in posts.find({"user_id": ObjectId(user_id)}).sort("created_at", -1)]

@app.post("/api/posts/{post_id}/like")
def like_post(post_id: str, user: dict = Depends(get_current_user_sync)):
    print(f"[LIKE] post_id={post_id}, user_id={user['_id']}")
    post = posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        print(f"[LIKE] Post not found: {post_id}")
        raise HTTPException(status_code=404, detail="Post not found")
    user_id = str(user["_id"])
    if user_id in post.get("likes", []):
        # Unlike
        result = posts.update_one({"_id": ObjectId(post_id)}, {"$pull": {"likes": user_id}})
        print(f"[LIKE] Unlike result: {result.raw_result}")
        liked = False
    else:
        # Like
        result = posts.update_one({"_id": ObjectId(post_id)}, {"$addToSet": {"likes": user_id}})
        print(f"[LIKE] Like result: {result.raw_result}")
        liked = True
    updated_post = posts.find_one({"_id": ObjectId(post_id)})
    if updated_post:
        print(f"[LIKE] Updated post.likes: {updated_post.get('likes', [])}")
        return {"liked": liked, "likes": updated_post.get("likes", [])}
    else:
        print(f"[LIKE] Updated post not found after like/unlike: {post_id}")
        return {"liked": liked, "likes": []}

@app.post("/api/posts/{post_id}/comment")
def comment_post(post_id: str, text: str = Body(...), user: dict = Depends(get_current_user_sync)):
    post = posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    comment = {
        "user_id": str(user["_id"]),
        "text": text,
        "created_at": datetime.utcnow()
    }
    posts.update_one({"_id": ObjectId(post_id)}, {"$push": {"comments": comment}})
    updated_post = posts.find_one({"_id": ObjectId(post_id)})
    if updated_post:
        return {"comments": updated_post.get("comments", [])}
    else:
        return {"comments": []}

@app.put("/api/posts/{post_id}", response_model=PostOut)
def update_post(
    post_id: str,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    tags: str = Form(""),
    file: UploadFile = File(None),
    current_user: dict = Depends(get_current_user_sync)
):
    post = posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if str(post["user_id"]) != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="Not authorized to edit this post")
    update_fields = {
        "title": title,
        "description": description,
        "category": category,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
    }
    if file:
        import cloudinary.uploader
        if file.content_type == "application/pdf":
            result = cloudinary.uploader.upload(file.file, resource_type="raw", filename=file.filename)
            update_fields["file_type"] = "pdf"
        elif file.content_type and file.content_type.startswith("image/"):
            result = cloudinary.uploader.upload(file.file, resource_type="image", filename=file.filename)
            update_fields["file_type"] = file.content_type.split("/")[1]
        else:
            result = cloudinary.uploader.upload(file.file, resource_type="auto", filename=file.filename)
            update_fields["file_type"] = file.content_type if file.content_type else None
        update_fields["file_url"] = result["secure_url"]
        update_fields["original_name"] = file.filename
    posts.update_one({"_id": ObjectId(post_id)}, {"$set": update_fields})
    updated_post = posts.find_one({"_id": ObjectId(post_id)})
    return post_to_dict(updated_post)

@app.delete("/api/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(post_id: str, user: dict = Depends(get_current_user_sync)):
    post = posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if str(post["user_id"]) != str(user["_id"]):
        raise HTTPException(status_code=403, detail="Not authorized to delete this post")
    posts.delete_one({"_id": ObjectId(post_id)})
    return