"""
執行這個腳本建立第一個 symotus_admin 帳號
用法: python init_admin.py
"""
from database import SessionLocal
from models import User, Base
from database import engine
from auth import hash_password

Base.metadata.create_all(bind=engine)

db = SessionLocal()
existing = db.query(User).filter(User.role == "symotus_admin").first()
if existing:
    print(f"Admin already exists: {existing.username}")
else:
    admin = User(
        username="symotus_admin",
        email="admin@symotus.com",
        full_name="Symotus Admin",
        hashed_password=hash_password("change-this-password"),
        role="symotus_admin",
        is_active=True,
    )
    db.add(admin)
    db.commit()
    print("Admin created: symotus_admin / change-this-password")
    print("Please change the password immediately!")
db.close()
